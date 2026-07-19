"""DEC-TIMESTAMP-IST-SINGLESOURCE-001 — the IST/session single-source laws.

Three law families, one file:

1. **Root laws** — the canonical roots in :mod:`brutex.calendars.nse`
   (``IST`` · ``IST_TZ_NAME`` · ``IST_UTC_OFFSET`` · ``IST_OFFSET_MINUTES`` ·
   ``IST_FIXED_OFFSET`` · ``SESSION_OPEN_IST`` · ``SESSION_CLOSE_IST``) hold
   their literal values and their derivations are internally consistent,
   including the cross-FAMILY same-offset equality (ZoneInfo vs fixed-offset —
   the two are deliberately distinct objects, never interchanged; only their
   ``utcoffset`` must agree).

2. **Per-swap equality laws** — every production module whose private IST /
   session constant was collapsed onto a root still binds the EXACT same value
   (identity where the object is shared; equality + the pre-existing
   representation detail where a derivation is kept — the former
   ``data/parser`` ``name="IST"`` example died with that module's delete,
   DEC-DATA-PARSER-DEAD-DELETE-001).

3. **Cross-language drift pins** — the Rust / JS / SQL copies CANNOT import
   Python, so their literals are pinned against the Python roots by reading
   the source files (the ``test_gpu_gate_drift_pin`` house pattern): a future
   change to any one side fails CI unless all sides move together.

Deliberately NOT unified (documented, pinned): the F&O expiry-INSTANT law
(``greeks_enrich.EXPIRY_HOUR_IST``/``EXPIRY_MINUTE_IST``) merely EQUALS the
15:30 session close — a distinct contract-expiry convention; and the
minute-bound SEMANTIC layer (375-bar counts, minute_of_session, 15:29
close-edge logic, ``_ist_minute_of_day_expr``) is a separate data-gated
program (named follow-up in MIGRATION.md #1929), untouched here.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from brutex.calendars.nse import (
    IST,
    IST_FIXED_OFFSET,
    IST_OFFSET_MINUTES,
    IST_TZ_NAME,
    IST_UTC_OFFSET,
    PRE_OPEN_END_IST,
    SESSION_CLOSE_IST,
    SESSION_OPEN_IST,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ─────────────────────────────────────────────────────────────────────────────
# 1 · Root laws
# ─────────────────────────────────────────────────────────────────────────────


class TestRootLaws:
    """The canonical roots hold their literal values + derivation consistency."""

    def test_ist_tz_name_literal(self) -> None:
        assert IST_TZ_NAME == "Asia/Kolkata"

    def test_ist_zoneinfo_derives_from_the_string_root(self) -> None:
        assert isinstance(IST, ZoneInfo)
        assert IST.key == IST_TZ_NAME

    def test_ist_utc_offset_literal(self) -> None:
        assert timedelta(hours=5, minutes=30) == IST_UTC_OFFSET

    def test_ist_offset_minutes_derives_exactly(self) -> None:
        assert IST_OFFSET_MINUTES == 330
        assert int(IST_UTC_OFFSET.total_seconds()) // 60 == IST_OFFSET_MINUTES
        # the offset is a whole number of minutes (the // 60 loses nothing)
        assert timedelta(minutes=IST_OFFSET_MINUTES) == IST_UTC_OFFSET

    def test_fixed_offset_family_root(self) -> None:
        assert timezone(timedelta(hours=5, minutes=30)) == IST_FIXED_OFFSET
        assert IST_FIXED_OFFSET.utcoffset(None) == IST_UTC_OFFSET
        # the unnamed fixed-offset representation (the deleted data/parser kept its own
        # named derivation — see TestPerSwapEquality)
        assert IST_FIXED_OFFSET.tzname(None) == "UTC+05:30"

    @pytest.mark.parametrize(
        "instant",
        [
            datetime(2020, 1, 1, 3, 45, tzinfo=UTC),
            datetime(2023, 7, 15, 12, 0, tzinfo=UTC),  # a northern-summer date (no DST)
            datetime(2026, 12, 31, 18, 30, tzinfo=UTC),
        ],
    )
    def test_zoneinfo_and_fixed_offset_families_agree_on_utcoffset(self, instant: datetime) -> None:
        # The cross-FAMILY equality law: Asia/Kolkata is a constant +05:30 zone
        # (no DST since 1945), so for every covered instant the ZoneInfo root's
        # utcoffset equals the fixed-offset root's — the two representations
        # denote the SAME instant shift while remaining distinct objects.
        assert instant.astimezone(IST).utcoffset() == IST_UTC_OFFSET
        assert instant.astimezone(IST_FIXED_OFFSET).utcoffset() == IST_UTC_OFFSET
        assert instant.astimezone(IST) == instant.astimezone(IST_FIXED_OFFSET)

    def test_families_are_deliberately_distinct_objects(self) -> None:
        # NOT interchangeable: tzname/repr differ — a swap across families
        # would be observable, so each family keeps its own root.
        sample = datetime(2024, 1, 2, 9, 15, tzinfo=IST)
        assert sample.tzname() == "IST"
        assert datetime(2024, 1, 2, 9, 15, tzinfo=IST_FIXED_OFFSET).tzname() == "UTC+05:30"

    def test_session_roots(self) -> None:
        assert time(9, 15) == SESSION_OPEN_IST
        assert time(15, 30) == SESSION_CLOSE_IST
        # the pre-open auction END is the session OPEN — derived, same object
        assert PRE_OPEN_END_IST is SESSION_OPEN_IST


# ─────────────────────────────────────────────────────────────────────────────
# 2 · Per-swap equality laws (every collapsed production constant)
# ─────────────────────────────────────────────────────────────────────────────


class TestPerSwapEquality:
    """Each collapsed module constant binds exactly the root's value."""

    def test_data_session_reexports(self) -> None:
        # HARD CONSTRAINT (TF-registry PR #1904 imports these BY NAME): the
        # names stay importable from brutex.data.session and equal the roots.
        from brutex.data import session

        assert session.IST is IST
        assert session.SESSION_OPEN is SESSION_OPEN_IST
        assert session.SESSION_CLOSE is SESSION_CLOSE_IST
        assert time(9, 15) == session.SESSION_OPEN
        assert time(15, 30) == session.SESSION_CLOSE

    def test_zoneinfo_family_consumers_bind_the_root(self) -> None:
        # (The historical quota_tracker._IST probe died with the dormant
        # tracker — DEC-ORCH-DEAD-SCAFFOLD-DELETE-001, MIGRATION.md #1969:
        # the trimmed module is clock-free, importing neither ZoneInfo nor
        # calendars.nse.)
        from brutex.crossverify import config as cv_config
        from brutex.dashboard import db_view

        assert cv_config._IST is IST
        assert db_view._IST is IST

    def test_ingest_common_binds_the_roots(self) -> None:
        from brutex.adapters import ingest_common

        assert ingest_common._IST_ZONE is IST
        assert ingest_common._IST is IST
        assert ingest_common._MARKET_CLOSE_IST is SESSION_CLOSE_IST
        assert ingest_common._SESSION_OPEN is SESSION_OPEN_IST
        assert ingest_common._SYNTHETIC_CAPABILITIES.native_timezone == IST_TZ_NAME

    def test_string_family_consumers_bind_the_root(self) -> None:
        from brutex.dashboard import movers_stored_read
        from brutex.data import bar_screen

        assert movers_stored_read._IST == IST_TZ_NAME
        assert bar_screen._IST_TZ == IST_TZ_NAME

    def test_fixed_offset_family_consumers_bind_the_root(self) -> None:
        from brutex.adapters.groww import epoch_verdict
        from brutex.adapters.groww import parser as groww_parser
        from brutex.backup import run as backup_run
        from brutex.data import greeks_backfill, greeks_enrich, lake_heal, spot_daily_resample

        assert groww_parser._IST is IST_FIXED_OFFSET
        assert epoch_verdict._IST_OFFSET is IST_UTC_OFFSET
        assert greeks_enrich._IST is IST_FIXED_OFFSET
        assert greeks_backfill._IST is IST_FIXED_OFFSET
        assert lake_heal._IST is IST_FIXED_OFFSET
        assert spot_daily_resample._IST is IST_FIXED_OFFSET
        assert backup_run._IST is IST_FIXED_OFFSET

    # NOTE: the former test_data_parser_keeps_its_named_tzname_derived_offset
    # pin died with its subject — the dead ``brutex/data/parser.py`` module was
    # deleted (DEC-DATA-PARSER-DEAD-DELETE-001, MIGRATION.md #1941; zero
    # production consumers — the delete supersedes the #1929 representation
    # pin for that module only; every other family pin above is unchanged).

    def test_session_open_mirrors_bind_the_root(self) -> None:
        from brutex.dashboard import db_view
        from brutex.data import spot_features

        assert db_view._SESSION_OPEN_IST is SESSION_OPEN_IST
        assert spot_features._SESSION_OPEN is SESSION_OPEN_IST

    def test_sweep_scheduler_offset_seconds_derives(self) -> None:
        from brutex.engine.sweep_scheduler import IST_OFFSET_SECONDS

        assert IST_OFFSET_SECONDS == 19_800
        assert int(IST_UTC_OFFSET.total_seconds()) == IST_OFFSET_SECONDS

    def test_groww_vendor_native_timezone_fact_equals_the_root(self) -> None:
        # The §6 verified-vendor-fact constant stays a literal in
        # groww/constants.py (its file:line is citation-pinned); this equality
        # pin keeps it from ever drifting off the canonical string root.
        from brutex.adapters.groww.constants import NATIVE_TIMEZONE
        from brutex.adapters.synthetic.constants import NATIVE_TIMEZONE as SYN_NATIVE_TIMEZONE

        assert NATIVE_TIMEZONE == IST_TZ_NAME
        assert SYN_NATIVE_TIMEZONE == IST_TZ_NAME

    def test_expiry_instant_law_deliberately_not_unified(self) -> None:
        # The F&O expiry INSTANT (15:30 IST on expiry day) EQUALS the session
        # close by exchange reality but is a DISTINCT contract-expiry law —
        # deliberately NOT collapsed onto SESSION_CLOSE_IST (documented in
        # MIGRATION.md #1929). This pin records the equality without coupling.
        from brutex.data.greeks_enrich import EXPIRY_HOUR_IST, EXPIRY_MINUTE_IST

        assert (
            SESSION_CLOSE_IST.hour,
            SESSION_CLOSE_IST.minute,
        ) == (EXPIRY_HOUR_IST, EXPIRY_MINUTE_IST)

    def test_db_view_sql_interval_interpolates_the_root(self) -> None:
        # BYTE-IDENTITY: the interpolated SQL fragment equals the historical
        # literal text exactly.
        from brutex.dashboard.db_view import _IST_TS_EXPR

        assert _IST_TS_EXPR == "((\"timestamp\" AT TIME ZONE 'UTC') + INTERVAL '330 minutes')"
        assert f"INTERVAL '{IST_OFFSET_MINUTES} minutes'" in _IST_TS_EXPR


# ─────────────────────────────────────────────────────────────────────────────
# 3 · Cross-language drift pins (Rust / JS — cannot import Python)
# ─────────────────────────────────────────────────────────────────────────────


def _source(rel: str) -> str:
    path = _REPO_ROOT / rel
    assert path.is_file(), f"drift-pin target missing: {rel}"
    return path.read_text(encoding="utf-8")


class TestCrossLanguageDriftPins:
    """Rust/JS literals must equal the Python roots — change one, change all."""

    def test_brutex_feed_rust_ist_offset_us(self) -> None:
        src = _source("rust/brutex-feed/src/scan.rs")
        match = re.search(r"pub const IST_OFFSET_US: i64 = ([0-9_]+);", src)
        assert match is not None, "IST_OFFSET_US not found in scan.rs"
        rust_us = int(match.group(1).replace("_", ""))
        assert rust_us == IST_OFFSET_MINUTES * 60 * 1_000_000
        assert rust_us == int(IST_UTC_OFFSET.total_seconds()) * 1_000_000

    def test_fno_math_rust_ist_offset_constants(self) -> None:
        src = _source("rust/fno-math/src/compat/constants.rs")
        seconds = int(IST_UTC_OFFSET.total_seconds())
        m32 = re.search(r"pub const IST_UTC_OFFSET_SECONDS: i32 = ([0-9_]+);", src)
        m64 = re.search(r"pub const IST_UTC_OFFSET_SECONDS_I64: i64 = ([0-9_]+);", src)
        mns = re.search(r"pub const IST_UTC_OFFSET_NANOS: i64 = ([0-9_]+);", src)
        assert m32 is not None and m64 is not None and mns is not None
        assert int(m32.group(1).replace("_", "")) == seconds
        assert int(m64.group(1).replace("_", "")) == seconds
        assert int(mns.group(1).replace("_", "")) == seconds * 1_000_000_000

    def test_bx_chart_js_ist_literals(self) -> None:
        src = _source("brutex/dashboard/static/bx-chart.js")
        # the naive-string IST suffix — "+HH:MM" derived from the root offset
        hours, minutes = divmod(IST_OFFSET_MINUTES, 60)
        assert f's += "+{hours:02d}:{minutes:02d}";' in src
        # the epoch-ms IST-date shift — minutes derived from the root
        assert f"ms + {IST_OFFSET_MINUTES} * 60000" in src

    def test_bx_movers_time_js_session_minute_literals(self) -> None:
        src = _source("brutex/dashboard/static/bx-movers-time.js")
        open_expr = (
            f"var BX_SESSION_OPEN_MIN = {SESSION_OPEN_IST.hour} * 60 + {SESSION_OPEN_IST.minute};"
        )
        close_expr = f"var BX_SESSION_CLOSE_MIN = {SESSION_CLOSE_IST.hour} * 60 + {SESSION_CLOSE_IST.minute};"
        assert open_expr in src
        assert close_expr in src
