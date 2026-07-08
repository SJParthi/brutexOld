"""Unit tests for :mod:`brutex.data.futures_series` — the ADR-0012 series resolver.

DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001 / DEC-FUTURES-ADDRESSING-PIN-001: pins the
vendor-truth ownership law (``(E_{k-1}, E_k]``, expiry day owned by the
EXPIRING contract — the ``resolve_near_month`` ``>=`` law), the loud-refusal
vs graceful-abstain failure-mode matrix (interior hole refuses NAMING months —
including a window STARTING inside a hole; head spans stay UNOWNED + WARN;
ANY tail overhang WARNs), the observe-only calendar cross-check (drift WARNs
with evidence, coverage abstains — vendor dates govern), the returned evidence
fields (``unparsed_dirs`` / ``cross_checks`` — exact-equality-testable, never
log-only), and the ``MAX_MONTHLY_EXPIRY_GAP_DAYS`` bound against the REAL
2020-2026 monthly enumeration.

This file is the module's Layer-B/Layer-C covering set — it alone must hold
``brutex/data/futures_series.py`` at 100% line + 100% branch.
"""

from __future__ import annotations

import itertools
import os
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import brutex.universe
import pytest
from brutex.calendars.nse import HolidayDataUnavailableError
from brutex.data import futures_series
from brutex.data.errors import DataLakeError
from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.futures_series import (
    MAX_MONTHLY_EXPIRY_GAP_DAYS,
    CalendarCrossCheck,
    FuturesSeriesError,
    FuturesSeriesResolution,
    FuturesSeriesSegment,
    is_futures_series_symbol,
    resolve_futures_series,
)
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────

#: Real vendor-shaped 2025 NIFTY monthly contract dirs (holiday-shifted
#: expiries verified against the repo calendar: Jun 26 / Jul 31 / Aug 28).
_JUN = "NSE-NIFTY-26Jun25-FUT"
_JUL = "NSE-NIFTY-31Jul25-FUT"
_AUG = "NSE-NIFTY-28Aug25-FUT"


def _mk_lake(tmp_path: Path, *names: str) -> Path:
    """Create a lake root holding ``bars/NSE/FNO/<name>`` dirs; return the root."""
    root = tmp_path / "lake"
    fno = root / "bars" / "NSE" / "FNO"
    fno.mkdir(parents=True, exist_ok=True)
    for name in names:
        (fno / name).mkdir()
    return root


def _events(logs: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    """Filter captured structlog records down to one event name."""
    return [rec for rec in logs if rec["event"] == event]


# ────────────────────────────────────────────────────────────────────────
# The alias predicate + grammar collision-proofing
# ────────────────────────────────────────────────────────────────────────


class TestAliasPredicate:
    """`is_futures_series_symbol` — exact-case, import-identity, O(1)."""

    def test_alias_accepted(self) -> None:
        assert is_futures_series_symbol("NSE-NIFTY-FUT") is True

    @pytest.mark.parametrize(
        "symbol",
        [
            "NIFTY",
            "NSE-NIFTY",
            "NSE-NIFTY-30Sep25-FUT",  # per-contract lake dir (retired shape)
            "NIFTY-FUT-2026-07-30",  # retired series_key shape
            "nse-nifty-fut",  # lake keys are case-exact
            "NSE-NIFTY-FUT ",  # trailing space
            "",
        ],
    )
    def test_everything_else_refused(self, symbol: str) -> None:
        assert is_futures_series_symbol(symbol) is False

    def test_import_identity_with_universe_constant(self) -> None:
        # The module consumes brutex.universe.NIFTY_FUTURES_SERIES_SYMBOL —
        # no second literal exists to drift (DEC-FUTURES-ADDRESSING-PIN-001).
        assert (
            futures_series.NIFTY_FUTURES_SERIES_SYMBOL
            is brutex.universe.NIFTY_FUTURES_SERIES_SYMBOL
        )

    def test_alias_never_parses_as_a_contract_symbol(self) -> None:
        # Collision-proofing: the F&O grammar can never grow into the alias
        # (3 hyphen tokens < the 4-token future minimum).
        assert parse_fno_symbol("NSE-NIFTY-FUT") is None

    def test_error_is_a_data_lake_error(self) -> None:
        assert issubclass(FuturesSeriesError, DataLakeError)


# ────────────────────────────────────────────────────────────────────────
# Scan + parse + filter (evidence fields, poison dir, 4-digit-year trap)
# ────────────────────────────────────────────────────────────────────────


class TestScanAndFilter:
    """Vendor-dir scan: NIFTY FUT dirs kept, foreign skipped, junk = evidence."""

    def test_foreign_and_option_dirs_are_silently_skipped(self, tmp_path: Path) -> None:
        root = _mk_lake(
            tmp_path,
            _JUN,
            _JUL,
            "NSE-NIFTY-30Sep25-24000-CE",  # option leg — no -FUT suffix
            "NSE-BANKNIFTY-25Jun25-FUT",  # wrong underlying
            "BSE-NIFTY-30Jul26-FUT",  # wrong embedded exchange
        )
        res = resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 7, 31))
        assert res.contract_symbols == (_JUN, _JUL)
        # Parseable-but-foreign dirs are NOT junk evidence — they are simply
        # out of the series (never WARNed, never guessed).
        assert res.unparsed_dirs == ()

    def test_unparseable_fut_dirs_become_sorted_evidence_and_warn(self, tmp_path: Path) -> None:
        root = _mk_lake(
            tmp_path,
            _JUN,
            "NSE-NIFTY-26Jun2024-FUT",  # the in-repo 4-digit-year trap
            "NSE-NIFTY-BADTOKEN-FUT",
        )
        with capture_logs() as logs:
            res = resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 6, 26))
        assert res.unparsed_dirs == ("NSE-NIFTY-26Jun2024-FUT", "NSE-NIFTY-BADTOKEN-FUT")
        warned = _events(logs, "futures_series.unparsed_dir")
        # repr()-named (the #907/#919 convention): the on-disk name is hostile-
        # controlled input — newline/ANSI bytes must not forge log lines.
        assert {rec["dir"] for rec in warned} == {repr(name) for name in res.unparsed_dirs}

    def test_regular_file_shaped_like_a_contract_is_skipped_and_warned(
        self, tmp_path: Path
    ) -> None:
        # Review round-1 finding 7: a stray regular FILE named like a contract
        # must never become an owning segment (it would poison the ownership
        # windows + roll_schedule, then dead-end the loader with a wrong
        # "re-pull" remediation).
        root = _mk_lake(tmp_path, _JUN, _JUL)
        stray = root / "bars" / "NSE" / "FNO" / "NSE-NIFTY-28Oct25-FUT"
        stray.write_bytes(b"not a directory")
        # Fails-before: pre-fix the stray FILE parsed into `ordered`, was
        # SELECTED for this window (own_start 2025-08-01 <= end) and the
        # Jul→Oct 89-day pairwise gap refused the whole launch with a wrong
        # remediation. Post-fix it is skipped + WARNed and the resolution
        # succeeds on the real dirs.
        with capture_logs() as logs:
            res = resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 8, 10))
        assert res.contract_symbols == (_JUN, _JUL)
        assert res.unparsed_dirs == ()  # a non-dir is NOT parse-failure evidence
        warned = _events(logs, "futures_series.non_dir_entry")
        assert len(warned) == 1
        assert warned[0]["entry"] == repr("NSE-NIFTY-28Oct25-FUT")

    def test_symlink_shaped_like_a_contract_is_skipped_and_warned(self, tmp_path: Path) -> None:
        # Review round-3 finding 3: `entry.is_dir()` follows symlinks, so a
        # contract-named SYMLINK at a foreign dir (a manual lake reorg /
        # quarantine-restore leftover) was accepted as an owning segment and
        # its TARGET's bars served as NIFTY series bars under that expiry's
        # ownership window — the exact poisoning the stray-FILE guard exists
        # to prevent. Post-fix (`follow_symlinks=False`) it rides the same
        # skip + `non_dir_entry` WARN.
        root = _mk_lake(tmp_path, _JUN, _JUL, "NSE-BANKNIFTY-30Oct25-FUT")
        fno = root / "bars" / "NSE" / "FNO"
        link = fno / "NSE-NIFTY-28Oct25-FUT"
        link.symlink_to(fno / "NSE-BANKNIFTY-30Oct25-FUT", target_is_directory=True)
        # Fails-before: the link parsed + was SELECTED for this window and the
        # Jul→Oct 89-day pairwise gap refused the whole launch; post-fix the
        # resolution succeeds on the real dirs with the link skipped + WARNed.
        with capture_logs() as logs:
            res = resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 8, 10))
        assert res.contract_symbols == (_JUN, _JUL)
        assert res.unparsed_dirs == ()  # a symlink is NOT parse-failure evidence
        warned = _events(logs, "futures_series.non_dir_entry")
        assert len(warned) == 1
        assert warned[0]["entry"] == repr("NSE-NIFTY-28Oct25-FUT")

    def test_case_drifted_nifty_dir_is_warned_as_scan_evidence(self, tmp_path: Path) -> None:
        # Round-7 finding 4: a dir that PARSES but misses the series only by
        # letter case (a hand-copied 'NSE-nifty-28Aug25-FUT') fell in neither
        # scan bucket — dropped with ZERO WARN and zero evidence, unlike the
        # repr-WARNed unparsed dirs. If it held a month's only copy the
        # interior-hole gate still refused, but never named the drifted dir
        # sitting right there. Now: one repr-named case_drift_dir WARN (the
        # #911 key-drift diagnostic convention); still never a segment.
        drifted = "NSE-nifty-28Aug25-FUT"
        root = _mk_lake(tmp_path, _JUN, _JUL, drifted)
        with capture_logs() as logs:
            res = resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 7, 31))
        assert res.contract_symbols == (_JUN, _JUL)
        assert res.unparsed_dirs == ()  # it PARSED — not parse-failure evidence
        warned = _events(logs, "futures_series.case_drift_dir")
        assert len(warned) == 1
        assert warned[0]["dir"] == repr(drifted)

    def test_exact_case_foreign_dirs_emit_no_case_drift_warn(self, tmp_path: Path) -> None:
        # The WARN is scoped to CASE drift only: genuinely foreign parsed
        # dirs (BANKNIFTY, a BSE-embedded exchange) are simply out of the
        # series and stay silent — never noise, never guessed.
        root = _mk_lake(tmp_path, _JUN, "NSE-BANKNIFTY-25Jun25-FUT", "BSE-NIFTY-30Jul26-FUT")
        with capture_logs() as logs:
            res = resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 6, 26))
        assert res.contract_symbols == (_JUN,)
        assert _events(logs, "futures_series.case_drift_dir") == []

    def test_poison_alias_dir_is_ignored_not_resolved(self, tmp_path: Path) -> None:
        # F7: a hand-created literal bars/NSE/FNO/NSE-NIFTY-FUT/ dir parses to
        # None → counted as evidence, NEVER a segment (the alias never reaches
        # a path-building API).
        root = _mk_lake(tmp_path, _JUN, "NSE-NIFTY-FUT")
        res = resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 6, 26))
        assert "NSE-NIFTY-FUT" in res.unparsed_dirs
        assert "NSE-NIFTY-FUT" not in res.contract_symbols
        assert res.segments[0].contract_symbol == _JUN


# ────────────────────────────────────────────────────────────────────────
# Loud refusals
# ────────────────────────────────────────────────────────────────────────


class TestRefusals:
    """Every refusal is a FuturesSeriesError whose message IS the remediation."""

    def test_inverted_window_refused(self, tmp_path: Path) -> None:
        root = _mk_lake(tmp_path, _JUN)
        with pytest.raises(FuturesSeriesError, match="window is inverted"):
            resolve_futures_series(root, start=date(2025, 7, 1), end=date(2025, 6, 1))

    def test_missing_fno_tree_refused_naming_the_pipeline_pull(self, tmp_path: Path) -> None:
        with pytest.raises(FuturesSeriesError, match=r"no NSE/FNO lake tree.*Pull · full"):
            resolve_futures_series(
                tmp_path / "empty-lake", start=date(2025, 6, 1), end=date(2025, 6, 26)
            )

    def test_zero_nifty_fut_dirs_refused(self, tmp_path: Path) -> None:
        root = _mk_lake(tmp_path)  # FNO tree exists, no contract dirs at all
        with pytest.raises(FuturesSeriesError, match="zero NIFTY futures contract dirs"):
            resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 6, 26))

    def test_only_foreign_dirs_also_refused_as_zero(self, tmp_path: Path) -> None:
        root = _mk_lake(tmp_path, "NSE-BANKNIFTY-25Jun25-FUT")
        with pytest.raises(FuturesSeriesError, match="zero NIFTY futures contract dirs"):
            resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 6, 26))

    def test_duplicate_parsed_expiry_refused_naming_both_dirs(self, tmp_path: Path) -> None:
        # strptime month tokens are case-insensitive, so two DIFFERENT dir
        # names can decode to ONE expiry — an ambiguous lake refuses loudly.
        twin = "NSE-NIFTY-26JUN25-FUT"
        root = _mk_lake(tmp_path, _JUN, twin)
        with pytest.raises(FuturesSeriesError, match="ambiguous lake") as exc:
            resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 6, 26))
        assert _JUN in str(exc.value)
        assert twin in str(exc.value)
        assert "2025-06-26" in str(exc.value)

    def test_window_outside_all_ownership_refused_naming_the_span(self, tmp_path: Path) -> None:
        root = _mk_lake(tmp_path, _JUN, _JUL)
        with pytest.raises(FuturesSeriesError, match="owns any date") as exc:
            resolve_futures_series(root, start=date(2026, 1, 1), end=date(2026, 1, 31))
        assert "2025-06-26 → 2025-07-31" in str(exc.value)
        assert "Pull · full pipeline" in str(exc.value)

    def test_same_month_poison_contract_refused_naming_both_dirs(self, tmp_path: Path) -> None:
        # Round-5 finding 10: NIFTY futures are STRICTLY monthly, so a second
        # dir expiring in the SAME calendar month (a hand-planted/foreign dir
        # with a DIFFERENT parsed expiry) is always lake corruption — yet it
        # slid between the duplicate-expiry gate (identical expiry only) and
        # the continuity gates (gap > 40 days only), minting a bogus mid-month
        # roll_schedule entry (an extra Stage-1A D11 hard reset + a re-keyed
        # run identity) with only the OBSERVE-only expiry_drift WARN — fully
        # silent on a calendar-abstaining 2027+ window. Now: loud refusal.
        poison = "NSE-NIFTY-15Sep25-FUT"
        root = _mk_lake(tmp_path, _AUG, poison, "NSE-NIFTY-30Sep25-FUT")
        with pytest.raises(FuturesSeriesError, match="strictly monthly") as exc:
            resolve_futures_series(root, start=date(2025, 9, 1), end=date(2025, 9, 30))
        message = str(exc.value)
        assert "ambiguous lake" in message
        assert poison in message
        assert "NSE-NIFTY-30Sep25-FUT" in message
        assert "2025-09" in message
        assert "2025-09-15" in message and "2025-09-30" in message
        assert "quarantine" in message  # the remediation

    def test_same_month_poison_refused_on_a_calendar_abstaining_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The silent arm of finding 10: with the calendar cross-check
        # abstaining (2027+ horizon), even the observe-only expiry_drift WARN
        # vanished — the refusal must be calendar-INDEPENDENT.
        monkeypatch.setattr(futures_series, "_calendar_active_expiry", _raise_holiday_unavailable)
        root = _mk_lake(tmp_path, "NSE-NIFTY-10Sep27-FUT", "NSE-NIFTY-30Sep27-FUT")
        with pytest.raises(FuturesSeriesError, match="strictly monthly"):
            resolve_futures_series(root, start=date(2027, 9, 1), end=date(2027, 9, 30))

    def test_same_month_poison_refused_when_sibling_falls_outside_selection(
        self, tmp_path: Path
    ) -> None:
        # Round-7 finding 2: the round-5 gate compared SELECTED pairs only —
        # a window ending BEFORE the legitimate same-month contract's
        # ownership begins (July 16, the day after the poison's stolen
        # expiry) selected ONLY the poison, so the pairwise check never saw
        # the conflict and the poison dir's bars were served as series bars
        # under a bogus roll_schedule with only the observe-only drift WARN.
        # The gate now runs over the FULL scanned dir set.
        poison = "NSE-NIFTY-15Jul25-FUT"
        root = _mk_lake(tmp_path, _JUN, poison, _JUL, _AUG)
        with pytest.raises(FuturesSeriesError, match="strictly monthly") as exc:
            resolve_futures_series(root, start=date(2025, 7, 10), end=date(2025, 7, 14))
        message = str(exc.value)
        assert poison in message
        assert _JUL in message
        assert "quarantine" in message

    def test_same_month_poison_refused_as_sole_selected_segment(self, tmp_path: Path) -> None:
        # Round-7 finding 6: a window falling WHOLLY inside the poison's
        # stolen ownership span (May 30 → Jun 10) makes the poison the ONLY
        # selected segment — `itertools.pairwise` over one segment is empty,
        # so the round-5 gate never fired at all. Whole-lake check refuses.
        poison = "NSE-NIFTY-10Jun25-FUT"
        root = _mk_lake(tmp_path, "NSE-NIFTY-29May25-FUT", poison, _JUN)
        with pytest.raises(FuturesSeriesError, match="strictly monthly") as exc:
            resolve_futures_series(root, start=date(2025, 6, 2), end=date(2025, 6, 9))
        message = str(exc.value)
        assert poison in message
        assert _JUN in message

    def test_same_month_poison_outside_the_window_still_refuses(self, tmp_path: Path) -> None:
        # The widened gate is deliberately WHOLE-LAKE (never window-scoped):
        # NIFTY futures are strictly monthly, so a same-month duplicate
        # anywhere is lake corruption — refusing forces the quarantine even
        # when today's window would not touch the poisoned month (a later
        # window silently would). Structurally vendor-impossible, so a
        # legitimate lake can never false-refuse.
        poison = "NSE-NIFTY-15Sep25-FUT"
        root = _mk_lake(tmp_path, _JUN, _JUL, _AUG, poison, "NSE-NIFTY-30Sep25-FUT")
        with pytest.raises(FuturesSeriesError, match="strictly monthly"):
            resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 6, 26))


class TestInteriorHoleGate:
    """F1 — the worst silent-corruption class ALWAYS refuses, naming months."""

    def test_missing_middle_months_refused_named_by_the_real_calendar(self, tmp_path: Path) -> None:
        # Jun + Sep dirs with Jul/Aug absent: the gap trigger is vendor-derived
        # (91 days > 40) and the NAMING walks the real repo calendar.
        root = _mk_lake(tmp_path, _JUN, "NSE-NIFTY-25Sep25-FUT")
        with pytest.raises(FuturesSeriesError, match="INTERIOR hole") as exc:
            resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 9, 25))
        message = str(exc.value)
        assert "the Jul 2025, Aug 2025 monthly expiry(ies)" in message
        assert _JUN in message
        assert "NSE-NIFTY-25Sep25-FUT" in message
        assert "NOT maskable by --allow-missing-slices" in message
        assert "Pull · full pipeline" in message

    def test_naming_abstains_to_the_day_gap_on_calendar_coverage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The gap TRIGGER never depends on calendar coverage — when the naming
        # walk cannot answer, the refusal still fires with the day-gap count.
        monkeypatch.setattr(
            futures_series,
            "_calendar_active_expiry",
            _raise_holiday_unavailable,
        )
        root = _mk_lake(tmp_path, _JUN, "NSE-NIFTY-25Sep25-FUT")
        with pytest.raises(
            FuturesSeriesError, match=r"a 91-day expiry gap \(calendar naming unavailable\)"
        ):
            resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 9, 25))

    def test_gap_at_the_bound_is_accepted_one_past_refused(self, tmp_path: Path) -> None:
        # Boundary teeth for MAX_MONTHLY_EXPIRY_GAP_DAYS = 40: exactly 40 days
        # passes the gate; 41 refuses (kills a > → >= mutant).
        ok_root = _mk_lake(tmp_path / "ok", "NSE-NIFTY-01Jul25-FUT", "NSE-NIFTY-10Aug25-FUT")
        res = resolve_futures_series(ok_root, start=date(2025, 7, 1), end=date(2025, 8, 10))
        assert (res.segments[1].expiry - res.segments[0].expiry).days == MAX_MONTHLY_EXPIRY_GAP_DAYS
        bad_root = _mk_lake(tmp_path / "bad", "NSE-NIFTY-01Jul25-FUT", "NSE-NIFTY-11Aug25-FUT")
        with pytest.raises(FuturesSeriesError, match="INTERIOR hole") as exc:
            resolve_futures_series(bad_root, start=date(2025, 7, 1), end=date(2025, 8, 11))
        # The real calendar CAN name the July expiry the 41-day gap skipped.
        assert "Jul 2025" in str(exc.value)


class TestInteriorHoleStartSideGate:
    """Review round-1 findings 1/10 — a window STARTING inside a hole refuses.

    The pairwise gate only sees SELECTED segments; when the pre-hole
    contract's expiry precedes the window start it is excluded from selection,
    so pre-fix the hole was silently served by the successor's back-month bars
    (no refusal, ``head_clamped=False``, single-entry ``roll_schedule``).
    """

    def test_window_starting_inside_a_hole_refuses_naming_the_month(self, tmp_path: Path) -> None:
        # Finding 1's repro: May + Jul dirs, June MISSING, window starts Jun 2
        # (inside the June hole) — pre-fix this resolved WITHOUT refusal to one
        # July segment owning 2025-06-02 → 2025-07-31.
        root = _mk_lake(tmp_path, "NSE-NIFTY-29May25-FUT", _JUL)
        with pytest.raises(FuturesSeriesError, match="INTERIOR hole") as exc:
            resolve_futures_series(root, start=date(2025, 6, 2), end=date(2025, 7, 31))
        message = str(exc.value)
        assert "Jun 2025" in message
        assert "NSE-NIFTY-29May25-FUT" in message
        assert _JUL in message

    def test_finding10_repro_mar_hole_refuses(self, tmp_path: Path) -> None:
        # Finding 10's repro: Jan/Feb/Apr dirs, Mar-2025 MISSING, window starts
        # 2025-03-05 → the Feb predecessor is dropped by selection and the Apr
        # contract silently served March pre-fix.
        root = _mk_lake(
            tmp_path,
            "NSE-NIFTY-30Jan25-FUT",
            "NSE-NIFTY-27Feb25-FUT",
            "NSE-NIFTY-24Apr25-FUT",
        )
        with pytest.raises(FuturesSeriesError, match="INTERIOR hole") as exc:
            resolve_futures_series(root, start=date(2025, 3, 5), end=date(2025, 4, 20))
        message = str(exc.value)
        assert "Mar 2025" in message
        assert "NSE-NIFTY-27Feb25-FUT" in message
        assert "NSE-NIFTY-24Apr25-FUT" in message

    def test_control_same_hole_with_predecessor_selected_still_refuses(
        self, tmp_path: Path
    ) -> None:
        # Control: the SAME holed lake with a window start that keeps the
        # predecessor selected refuses via the ORIGINAL pairwise gate.
        root = _mk_lake(tmp_path, "NSE-NIFTY-29May25-FUT", _JUL)
        with pytest.raises(FuturesSeriesError, match="INTERIOR hole"):
            resolve_futures_series(root, start=date(2025, 5, 1), end=date(2025, 7, 31))

    def test_predecessor_gap_at_the_bound_is_accepted(self, tmp_path: Path) -> None:
        # Boundary teeth for the START-side gate: a predecessor exactly
        # MAX_MONTHLY_EXPIRY_GAP_DAYS before the first selected expiry passes
        # (kills a > → >= mutant on the new gate); a healthy complete lake's
        # mid-month window start never false-refuses.
        root = _mk_lake(tmp_path, "NSE-NIFTY-01Jul25-FUT", "NSE-NIFTY-10Aug25-FUT")
        res = resolve_futures_series(root, start=date(2025, 7, 15), end=date(2025, 8, 10))
        assert res.contract_symbols == ("NSE-NIFTY-10Aug25-FUT",)
        assert (date(2025, 8, 10) - date(2025, 7, 1)).days == MAX_MONTHLY_EXPIRY_GAP_DAYS


def _raise_holiday_unavailable(on_date: date) -> date:
    """Monkeypatch stand-in: every calendar query hits the coverage horizon."""
    raise HolidayDataUnavailableError(f"no holiday data covering {on_date.isoformat()}")


# ────────────────────────────────────────────────────────────────────────
# Ownership windows (the (E_{k-1}, E_k] law) + schedule derivation
# ────────────────────────────────────────────────────────────────────────


class TestOwnershipWindows:
    """Vendor-truth windows partition the date axis; expiry day is INCLUSIVE."""

    def test_expiry_day_owned_by_the_expiring_contract(self, tmp_path: Path) -> None:
        # The >= law: own_end == expiry (inclusive) and the successor starts
        # the NEXT day — kills an off-by-one / >=→> mutant on the boundary.
        root = _mk_lake(tmp_path, _JUN, _JUL, _AUG)
        res = resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 8, 28))
        jun, jul, aug = res.segments
        assert jun.own_end == date(2025, 6, 26) == jun.expiry
        assert jul.own_start == date(2025, 6, 27)
        assert jul.own_end == date(2025, 7, 31) == jul.expiry
        assert aug.own_start == date(2025, 8, 1)
        assert aug.own_end == date(2025, 8, 28) == aug.expiry

    def test_every_window_date_owned_by_exactly_one_segment(self, tmp_path: Path) -> None:
        # P3 ownership partition — weekends/holidays included (date axis, not
        # trading-day axis).
        root = _mk_lake(tmp_path, _JUN, _JUL, _AUG)
        start, end = date(2025, 6, 1), date(2025, 8, 28)
        res = resolve_futures_series(root, start=start, end=end)
        current = start
        while current <= end:
            owners = [seg for seg in res.segments if seg.own_start <= current <= seg.own_end]
            assert len(owners) == 1, f"{current} owned by {len(owners)} segments"
            current = date.fromordinal(current.toordinal() + 1)

    def test_contracts_outside_the_window_are_not_selected(self, tmp_path: Path) -> None:
        # Both skip arms: the Jun contract's expiry predates the window start
        # AND the Aug contract's ownership begins after the window end.
        root = _mk_lake(tmp_path, _JUN, _JUL, _AUG)
        res = resolve_futures_series(root, start=date(2025, 7, 1), end=date(2025, 7, 20))
        assert res.contract_symbols == (_JUL,)
        seg = res.segments[0]
        # The predecessor comes from the FULL scanned set (vendor truth): the
        # unselected Jun dir still supplies own_start = its expiry + 1 day,
        # then the window clamps it in.
        assert seg.own_start == date(2025, 7, 1)
        assert seg.own_end == date(2025, 7, 20)  # clamped INTO the window

    def test_roll_schedule_is_the_owning_expiry_ordinals(self, tmp_path: Path) -> None:
        root = _mk_lake(tmp_path, _JUN, _JUL, _AUG)
        res = resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 8, 28))
        assert res.roll_schedule == tuple(seg.expiry.toordinal() for seg in res.segments)
        assert list(res.roll_schedule) == sorted(set(res.roll_schedule))

    def test_resolution_is_deterministic(self, tmp_path: Path) -> None:
        root = _mk_lake(tmp_path, _JUN, _JUL, "NSE-NIFTY-26Jun2024-FUT")
        first = resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 7, 31))
        second = resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 7, 31))
        assert first == second

    def test_scandir_order_independence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The resolver sorts by parsed expiry — a reversed directory listing
        # yields the identical resolution.
        root = _mk_lake(tmp_path, _JUL, _JUN, _AUG)
        baseline = resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 8, 28))
        real_scandir = os.scandir

        @contextmanager
        def reversed_scandir(path: Path) -> Generator[Iterator[os.DirEntry[str]]]:
            with real_scandir(path) as entries:
                collected = sorted(entries, key=lambda entry: entry.name, reverse=True)
            yield iter(collected)

        monkeypatch.setattr(os, "scandir", reversed_scandir)
        reordered = resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 8, 28))
        assert reordered == baseline


# ────────────────────────────────────────────────────────────────────────
# Head law (§2.4): vendor predecessor → calendar fallback → abstain clamp
# ────────────────────────────────────────────────────────────────────────


class TestHeadLaw:
    """Head spans are GRACEFUL (stay unowned + WARN) — never a refusal."""

    def test_first_lake_dir_gets_the_calendar_fallback_own_start(self, tmp_path: Path) -> None:
        # The very first dir in the whole lake: own_start = the previous
        # month's calendar expiry + 1 day (2025-05-29 + 1 → 2025-05-30).
        root = _mk_lake(tmp_path, _JUN)
        res = resolve_futures_series(root, start=date(2025, 5, 30), end=date(2025, 6, 26))
        assert res.segments[0].own_start == date(2025, 5, 30)
        assert res.head_clamped is False  # start == natural own_start exactly

    def test_head_unowned_span_stays_unowned_and_warns(self, tmp_path: Path) -> None:
        # Window starts BEFORE the first owned date → one WARN + head_clamped
        # evidence, and the segment KEEPS its natural own_start (review
        # round-1 findings 14/18): extending ownership backwards would serve
        # the first contract's pre-front-month (far-month basis) bars as
        # series bars; the unowned span instead loads NOTHING — the true
        # spot-mirroring graceful law (F13).
        root = _mk_lake(tmp_path, _JUN)
        with capture_logs() as logs:
            res = resolve_futures_series(root, start=date(2025, 5, 1), end=date(2025, 6, 26))
        assert res.head_clamped is True
        assert res.segments[0].own_start == date(2025, 5, 30)  # natural, NOT the window start
        warned = _events(logs, "futures_series.head_unowned")
        assert len(warned) == 1
        assert warned[0]["window_start"] == "2025-05-01"
        assert warned[0]["first_owned"] == "2025-05-30"
        assert warned[0]["contract"] == _JUN
        assert warned[0]["unowned_days"] == 29

    def test_head_unowned_cross_check_stays_honest(self, tmp_path: Path) -> None:
        # Review round-1 finding 19: pre-fix the cross-check ran at the
        # CLAMPED own_start (the window start), so a routine head-unowned
        # launch emitted a FALSE `expiry_drift` although the lake and the
        # calendar AGREE. With the natural own_start kept, the check reads ok.
        root = _mk_lake(tmp_path, _JUN)
        with capture_logs() as logs:
            res = resolve_futures_series(root, start=date(2025, 5, 1), end=date(2025, 6, 26))
        assert res.cross_checks == (
            CalendarCrossCheck(expiry=date(2025, 6, 26), computed=date(2025, 6, 26), status="ok"),
        )
        assert _events(logs, "futures_series.expiry_drift") == []

    def test_pre_floor_calendar_abstain_clamps_silently(self, tmp_path: Path) -> None:
        # The Jan-2020 pre-floor edge: the previous-month (Dec-2019) calendar
        # computation abstains → ownership is bounded at
        # expiry − MAX_MONTHLY_EXPIRY_GAP_DAYS (2019-12-21), which PRECEDES the
        # 2020-01-01 floor start, so the intersection clamp lands on the window
        # start WITHOUT a head_unowned WARN — byte-identical to the pre-fix
        # behaviour at the GROWW_HISTORY_FLOOR (the review round-2 finding-8
        # byte-identity pin).
        root = _mk_lake(tmp_path, "NSE-NIFTY-30Jan20-FUT")
        with capture_logs() as logs:
            res = resolve_futures_series(root, start=date(2020, 1, 1), end=date(2020, 1, 30))
        assert res.segments[0].own_start == date(2020, 1, 1)
        assert res.head_clamped is False
        assert _events(logs, "futures_series.head_unowned") == []

    def test_calendar_abstain_never_extends_ownership_to_the_window_start(
        self, tmp_path: Path
    ) -> None:
        # Review round-2 finding 8 (MAJOR, fails-before/passes-after): a lake
        # whose earliest dirs sit BEYOND the holiday-table horizon (2027+ —
        # `_head_own_start` abstains on HolidayDataUnavailableError, not just
        # the pre-floor ValueError) must NEVER own the arbitrary window start.
        # Pre-fix, own_start = start silently owned 2026-10-01 → 2027-02-25
        # with head_clamped=False and ZERO WARN, serving the Feb-27 contract's
        # far-month bars across the missing Dec-26/Jan-27 roll gaps with no
        # Stage-1A hard reset (the exact D11 violation class). Post-fix the
        # abstain is bounded at expiry − MAX_MONTHLY_EXPIRY_GAP_DAYS and the
        # earlier span stays UNOWNED + WARNed.
        root = _mk_lake(tmp_path, "NSE-NIFTY-25Feb27-FUT", "NSE-NIFTY-25Mar27-FUT")
        with capture_logs() as logs:
            res = resolve_futures_series(root, start=date(2026, 10, 1), end=date(2027, 3, 25))
        bound = date(2027, 2, 25) - timedelta(days=MAX_MONTHLY_EXPIRY_GAP_DAYS)
        assert res.segments[0].own_start == bound  # 2027-01-16, NOT 2026-10-01
        assert res.head_clamped is True
        warned = _events(logs, "futures_series.head_unowned")
        assert len(warned) == 1
        assert warned[0]["window_start"] == "2026-10-01"
        assert warned[0]["first_owned"] == bound.isoformat()
        # The 2027+ horizon also abstains the observe-only cross-check.
        assert {check.status for check in res.cross_checks} == {"abstain"}

    def test_pre_floor_abstain_bounds_a_deeper_window_with_a_warn(self, tmp_path: Path) -> None:
        # The same bound on the pre-floor edge: a window reaching deeper than
        # expiry − MAX_MONTHLY_EXPIRY_GAP_DAYS is no longer silently owned —
        # the span before the bound stays UNOWNED and WARNs (pre-fix it was
        # silently adopted with no evidence).
        root = _mk_lake(tmp_path, "NSE-NIFTY-30Jan20-FUT")
        with capture_logs() as logs:
            res = resolve_futures_series(root, start=date(2019, 12, 1), end=date(2020, 1, 30))
        bound = date(2020, 1, 30) - timedelta(days=MAX_MONTHLY_EXPIRY_GAP_DAYS)
        assert res.segments[0].own_start == bound  # 2019-12-21
        assert res.head_clamped is True
        warned = _events(logs, "futures_series.head_unowned")
        assert len(warned) == 1
        assert warned[0]["unowned_days"] == (bound - date(2019, 12, 1)).days

    def test_vendor_predecessor_beats_the_calendar(self, tmp_path: Path) -> None:
        # A junk-dated predecessor DIR is still vendor truth: the successor's
        # own_start derives from the parsed dir set, never re-derived from the
        # calendar (vendor-dir-primary).
        root = _mk_lake(tmp_path, "NSE-NIFTY-05Jul25-FUT", "NSE-NIFTY-08Aug25-FUT")
        res = resolve_futures_series(root, start=date(2025, 7, 20), end=date(2025, 8, 8))
        assert res.contract_symbols == ("NSE-NIFTY-08Aug25-FUT",)
        # prev expiry 2025-07-05 + 1 day, then clamped into the window.
        assert res.segments[0].own_start == date(2025, 7, 20)


# ────────────────────────────────────────────────────────────────────────
# Tail law: ANY positive overhang recorded + WARNed (escalation flag past bound)
# ────────────────────────────────────────────────────────────────────────


class TestTailLaw:
    """A missing NEWEST contract is visible, never silent — and never a refusal."""

    def test_window_end_inside_ownership_clamps_own_end(self, tmp_path: Path) -> None:
        root = _mk_lake(tmp_path, _JUN)
        res = resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 6, 20))
        assert res.segments[-1].own_end == date(2025, 6, 20)
        assert res.tail_unowned_days == 0

    def test_small_overhang_warns_too(self, tmp_path: Path) -> None:
        # Review round-1 findings 4/12/20: ANY positive overhang means the
        # successor contract owning those dates is ABSENT from the lake, so a
        # 10-day truncation now WARNs (pre-fix it was fully silent, drifting
        # from the #1521 'clamp + WARN' law and this module's own docstring).
        root = _mk_lake(tmp_path, _JUN)
        with capture_logs() as logs:
            res = resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 7, 6))
        assert res.tail_unowned_days == 10
        assert res.segments[-1].own_end == date(2025, 6, 26)  # never past the expiry
        warned = _events(logs, "futures_series.tail_unowned")
        assert len(warned) == 1
        assert warned[0]["unowned_days"] == 10
        assert warned[0]["exceeds_monthly_gap"] is False

    def test_overhang_past_the_gap_bound_warns(self, tmp_path: Path) -> None:
        root = _mk_lake(tmp_path, _JUN)
        with capture_logs() as logs:
            res = resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 8, 10))
        assert res.tail_unowned_days == 45
        warned = _events(logs, "futures_series.tail_unowned")
        assert len(warned) == 1
        assert warned[0]["unowned_days"] == 45
        assert warned[0]["last_expiry"] == "2025-06-26"
        assert warned[0]["contract"] == _JUN
        assert warned[0]["exceeds_monthly_gap"] is True


# ────────────────────────────────────────────────────────────────────────
# Observe-only calendar cross-check (G4 drift / G5 coverage abstain)
# ────────────────────────────────────────────────────────────────────────


class TestCalendarCrossCheck:
    """Vendor dates govern; the calendar only ever WARNs with evidence."""

    def test_agreeing_lake_reads_all_ok(self, tmp_path: Path) -> None:
        root = _mk_lake(tmp_path, _JUN, _JUL, _AUG)
        res = resolve_futures_series(root, start=date(2025, 6, 1), end=date(2025, 8, 28))
        assert res.cross_checks == (
            CalendarCrossCheck(expiry=date(2025, 6, 26), computed=date(2025, 6, 26), status="ok"),
            CalendarCrossCheck(expiry=date(2025, 7, 31), computed=date(2025, 7, 31), status="ok"),
            CalendarCrossCheck(expiry=date(2025, 8, 28), computed=date(2025, 8, 28), status="ok"),
        )

    def test_drift_warns_naming_both_sides_and_nearest_parsed(self, tmp_path: Path) -> None:
        # A one-day-off vendor token (30Jul25 vs the computed 31Jul25): the
        # resolution PROCEEDS on the vendor date; the drift is WARNed with
        # BOTH sides + the nearest parsed expiries, and returned as a row.
        root = _mk_lake(tmp_path, "NSE-NIFTY-30Jul25-FUT")
        with capture_logs() as logs:
            res = resolve_futures_series(root, start=date(2025, 7, 1), end=date(2025, 7, 30))
        assert res.cross_checks == (
            CalendarCrossCheck(
                expiry=date(2025, 7, 30), computed=date(2025, 7, 31), status="drift"
            ),
        )
        assert res.segments[0].own_end == date(2025, 7, 30)  # vendor date governs
        warned = _events(logs, "futures_series.expiry_drift")
        assert len(warned) == 1
        assert warned[0]["parsed_expiry"] == "2025-07-30"
        assert warned[0]["computed_expiry"] == "2025-07-31"
        assert warned[0]["nearest_parsed"] == ["2025-07-30"]

    def test_coverage_horizon_abstains_with_the_real_calendar(self, tmp_path: Path) -> None:
        # The REAL G5 cliff: a Dec-2026 own_start makes the eager next-month
        # walk cross into 2027 → HolidayDataUnavailableError → the cross-check
        # ABSTAINS (WARN + status row); resolution proceeds vendor-date-total.
        root = _mk_lake(tmp_path, "NSE-NIFTY-24Nov26-FUT", "NSE-NIFTY-29Dec26-FUT")
        with capture_logs() as logs:
            res = resolve_futures_series(root, start=date(2026, 12, 1), end=date(2026, 12, 20))
        assert res.contract_symbols == ("NSE-NIFTY-29Dec26-FUT",)
        assert res.cross_checks == (
            CalendarCrossCheck(expiry=date(2026, 12, 29), computed=None, status="abstain"),
        )
        warned = _events(logs, "futures_series.calendar_coverage_abstain")
        assert len(warned) == 1
        assert warned[0]["contract"] == "NSE-NIFTY-29Dec26-FUT"

    def test_holiday_shifted_vendor_token_agrees_with_the_calendar(self, tmp_path: Path) -> None:
        # The holiday-shift edge: Jan-2023's calendar expiry (Thu 26 Jan,
        # Republic Day) shifts to Wed 25 Jan. A vendor dir carrying the
        # SHIFTED token cross-checks "ok"; the UNSHIFTED token drifts.
        shifted_root = _mk_lake(
            tmp_path / "shifted", "NSE-NIFTY-29Dec22-FUT", "NSE-NIFTY-25Jan23-FUT"
        )
        res = resolve_futures_series(shifted_root, start=date(2023, 1, 2), end=date(2023, 1, 25))
        assert res.cross_checks == (
            CalendarCrossCheck(expiry=date(2023, 1, 25), computed=date(2023, 1, 25), status="ok"),
        )
        unshifted_root = _mk_lake(
            tmp_path / "unshifted", "NSE-NIFTY-29Dec22-FUT", "NSE-NIFTY-26Jan23-FUT"
        )
        res2 = resolve_futures_series(unshifted_root, start=date(2023, 1, 2), end=date(2023, 1, 26))
        assert res2.cross_checks[0].status == "drift"
        assert res2.cross_checks[0].computed == date(2023, 1, 25)


# ────────────────────────────────────────────────────────────────────────
# The gap bound against the REAL 2020-2026 enumeration
# ────────────────────────────────────────────────────────────────────────


class TestGapBoundJustification:
    """MAX_MONTHLY_EXPIRY_GAP_DAYS sits strictly between real adjacent gaps
    and the smallest possible skipped-month gap (the constant's teeth)."""

    def test_bound_separates_adjacent_from_skipped_month_gaps(self) -> None:
        from brutex.data.futures_roll import resolve_active_future_expiry
        from brutex.options.lot_size_history import OptionsUnderlying

        chain: list[date] = [
            resolve_active_future_expiry(OptionsUnderlying.NIFTY, date(2020, 1, 1))
        ]
        while True:
            probe = date.fromordinal(chain[-1].toordinal() + 1)
            try:
                nxt = resolve_active_future_expiry(OptionsUnderlying.NIFTY, probe)
            except HolidayDataUnavailableError:
                break  # the 2027+ coverage horizon ends the walk honestly
            chain.append(nxt)
        assert len(chain) >= 80  # the full 2020 → late-2026 monthly ladder
        adjacent = [(b - a).days for a, b in itertools.pairwise(chain)]
        skipped = [(chain[i + 2] - chain[i]).days for i in range(len(chain) - 2)]
        # Every REAL adjacent gap fits under the bound (the gate never
        # false-fires on a healthy lake) …
        assert max(adjacent) <= MAX_MONTHLY_EXPIRY_GAP_DAYS
        # … and every one-month hole overshoots it (the gate always fires).
        assert min(skipped) > MAX_MONTHLY_EXPIRY_GAP_DAYS


# ────────────────────────────────────────────────────────────────────────
# Dataclass evidence shape
# ────────────────────────────────────────────────────────────────────────


class TestResolutionEvidenceShape:
    """The evidence carriers are frozen values — exact-equality-testable."""

    def test_segment_and_resolution_are_frozen(self) -> None:
        seg = FuturesSeriesSegment(
            contract_symbol=_JUN,
            expiry=date(2025, 6, 26),
            own_start=date(2025, 6, 1),
            own_end=date(2025, 6, 26),
        )
        with pytest.raises(AttributeError):
            seg.expiry = date(2025, 6, 27)  # type: ignore[misc]
        res = FuturesSeriesResolution(
            segments=(seg,),
            roll_schedule=(seg.expiry.toordinal(),),
            contract_symbols=(_JUN,),
            unparsed_dirs=(),
            cross_checks=(),
            head_clamped=False,
            tail_unowned_days=0,
        )
        with pytest.raises(AttributeError):
            res.head_clamped = True  # type: ignore[misc]
