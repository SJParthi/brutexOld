"""Flag-ON golden twins for the batch Rust Stage-1A arm.

DEC-STAGE1A-RUST-BATCH-001 §6.6 — the frozen Stage-1A / roll goldens replayed
with ``BRUTEX_SWEEP_RUST_STAGE1A=1`` against their BYTE-UNTOUCHED fixtures
(none regenerated, none edited), the six real Muhurat-boundary parity runs
(the #1616 corpus), and the G-5 killer pin: the frozen sweep
``_GOLDEN_CONTENT_HASH`` / ``_GOLDEN_RUN_ID`` suite replayed flag-ON
reproduces the pinned bytes with EXACTLY ONE ``rust_stage1a.engaged`` event —
the strongest end-to-end bit-identity net (the #1531/#1532 precedent).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.engine.rust_stage1a import (
    STAGE1A_RUST_ENV,
    reset_observability_memo_for_tests,
    reset_self_check_for_tests,
)
from brutex.engine.stage1a import Stage1AMatrixBuilder, build_bar_bits
from structlog.testing import capture_logs

# Frozen-oracle imports — the fixtures and pinned literals of the EXISTING
# golden suites, consumed read-only (the tests.unit cross-import precedent).
from tests.golden.test_engine_banknifty_week_golden import (
    _EXPECTED_SHA256 as _WEEK_SHA,  # pyright: ignore[reportPrivateUsage]
)
from tests.golden.test_engine_banknifty_week_golden import (
    _feed as _week_feed,  # pyright: ignore[reportPrivateUsage]
)
from tests.golden.test_engine_banknifty_week_golden import (
    _manifest as _week_manifest,  # pyright: ignore[reportPrivateUsage]
)
from tests.golden.test_engine_futures_roll_golden import (
    _EXPECTED_FIRST_POST_ROLL_MASK,  # pyright: ignore[reportPrivateUsage]
    _EXPECTED_LAST_PRE_ROLL_MASK,  # pyright: ignore[reportPrivateUsage]
    _HLC_SCRIPT,  # pyright: ignore[reportPrivateUsage]
    _ROLL_SCHEDULE,  # pyright: ignore[reportPrivateUsage]
    _load_fixture,  # pyright: ignore[reportPrivateUsage]
    _stitched_bars,  # pyright: ignore[reportPrivateUsage]
)
from tests.golden.test_engine_nifty_day_golden import (
    _EXPECTED_SHA256 as _DAY_SHA,  # pyright: ignore[reportPrivateUsage]
)
from tests.golden.test_engine_nifty_day_golden import (
    _feed as _day_feed,  # pyright: ignore[reportPrivateUsage]
)
from tests.golden.test_engine_nifty_day_golden import (
    _manifest as _day_manifest,  # pyright: ignore[reportPrivateUsage]
)
from tests.golden.test_engine_stage1a_golden import (
    _DAY1,  # pyright: ignore[reportPrivateUsage]
    _DAY2,  # pyright: ignore[reportPrivateUsage]
)


@pytest.fixture(autouse=True)
def _flag_on_fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test runs flag-ON from a fresh-process seam view."""
    reset_self_check_for_tests()
    reset_observability_memo_for_tests()
    monkeypatch.setenv(STAGE1A_RUST_ENV, "1")
    yield
    reset_self_check_for_tests()
    reset_observability_memo_for_tests()


def _assert_engaged(logs: Sequence[Mapping[str, object]]) -> None:
    """The anti-vacuous-gate teeth: the Rust arm genuinely served."""
    engaged = [e for e in logs if e.get("event") == "rust_stage1a.engaged"]
    assert len(engaged) == 1, "flag-ON replay silently fell back to Python"
    assert not [e for e in logs if e.get("event") == "rust_stage1a.kernel_autoreverted"]


def test_two_day_hand_derived_golden_flag_on() -> None:
    with capture_logs() as logs:
        masks = build_bar_bits([_DAY1, _DAY2])
    _assert_engaged(logs)
    assert masks == [3298534883328, 10376597024924540928]  # the frozen literals


def test_nifty_day_golden_manifest_flag_on() -> None:
    with capture_logs() as logs:
        masks = build_bar_bits(_day_feed())
    _assert_engaged(logs)
    assert _day_manifest(masks) == _DAY_SHA


def test_banknifty_week_golden_manifest_flag_on() -> None:
    with capture_logs() as logs:
        masks = build_bar_bits(_week_feed())
    _assert_engaged(logs)
    assert _week_manifest(masks) == _WEEK_SHA


def test_futures_roll_golden_fixture_flag_on() -> None:
    with capture_logs() as logs:
        masks = build_bar_bits(_stitched_bars(), roll_schedule=_ROLL_SCHEDULE)
    _assert_engaged(logs)
    fixture = _load_fixture()
    assert masks == fixture["masks"]  # the frozen v2 matrix (3-day reshape regen)
    n_pre = 2 * len(_HLC_SCRIPT)  # the pre-roll contract runs TWO script days
    assert masks[n_pre - 1] == _EXPECTED_LAST_PRE_ROLL_MASK
    assert masks[n_pre] == _EXPECTED_FIRST_POST_ROLL_MASK


# ────────────────────────────────────────────────────────────────────────
# The six real Muhurat boundaries (#1616 corpus) — flag-ON parity
# ────────────────────────────────────────────────────────────────────────

_MUHURAT_BOUNDARIES: tuple[tuple[date, date, date], ...] = (
    (date(2020, 11, 13), date(2020, 11, 14), date(2020, 11, 17)),
    (date(2021, 11, 3), date(2021, 11, 4), date(2021, 11, 8)),
    (date(2022, 10, 21), date(2022, 10, 24), date(2022, 10, 25)),
    (date(2023, 11, 10), date(2023, 11, 12), date(2023, 11, 13)),
    (date(2024, 10, 31), date(2024, 11, 1), date(2024, 11, 4)),
    (date(2025, 10, 20), date(2025, 10, 21), date(2025, 10, 23)),
)


def _day_bar(d: date, o: str, h: str, low: str, c: str) -> CandleBar:
    ts = datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC)  # 09:15 IST
    return CandleBar(
        timestamp=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=10,
        open_interest=None,
    )


@pytest.mark.parametrize(("prev_reg", "muhurat", "next_reg"), _MUHURAT_BOUNDARIES)
def test_muhurat_boundary_parity_flag_on(
    prev_reg: date, muhurat: date, next_reg: date, monkeypatch: pytest.MonkeyPatch
) -> None:
    bars = [
        _day_bar(prev_reg, "100.00", "110.00", "90.00", "105.00"),
        _day_bar(muhurat, "104.00", "104.50", "103.50", "104.20"),
        _day_bar(next_reg, "104.00", "104.00", "104.00", "104.00"),
    ]
    with capture_logs() as logs:
        on = build_bar_bits(bars)
    _assert_engaged(logs)
    monkeypatch.delenv(STAGE1A_RUST_ENV)
    assert on == build_bar_bits(bars)
    # The prior REGULAR session carried through the Muhurat boundary — the
    # prepass drives the REAL _roll_to_new_day, so the anchor is #1616-exact.
    builder = Stage1AMatrixBuilder()
    builder.collect_batch_inputs(bars)
    prev = builder._prev_day_ohlc  # pyright: ignore[reportPrivateUsage]
    assert prev is not None
    assert (prev.high_paisa, prev.low_paisa, prev.close_paisa) == (11000, 9000, 10500)


# ────────────────────────────────────────────────────────────────────────
# G-5 — the frozen sweep golden flag-ON (the #1532 killer pin)
# ────────────────────────────────────────────────────────────────────────


def test_frozen_sweep_golden_reproduced_flag_on_with_one_engaged_event(
    tmp_path: Path,
) -> None:
    """Flag-ON, the persisted sweep lands on the PINNED pre-Rust store hash
    byte-for-byte with exactly ONE ``rust_stage1a.engaged`` event — the
    strongest end-to-end bit-identity net (masks → Stage-1B → trades →
    metrics → persisted bytes)."""
    pytest.importorskip("polars")  # flush needs the [data] extra
    from brutex.engine.sweep_run import mask_run_id

    from tests.golden.test_engine_sweep_run_golden import (
        _CTX,  # pyright: ignore[reportPrivateUsage]
        _GOLDEN_CONTENT_HASH,  # pyright: ignore[reportPrivateUsage]
        _GOLDEN_RUN_ID,  # pyright: ignore[reportPrivateUsage]
        _PARAMS,  # pyright: ignore[reportPrivateUsage]
        _run,  # pyright: ignore[reportPrivateUsage]
    )

    with capture_logs() as logs:
        summary = _run(tmp_path, "flag_on")
    engaged = [e for e in logs if e.get("event") == "rust_stage1a.engaged"]
    assert len(engaged) == 1
    assert not [e for e in logs if e.get("event") == "rust_stage1a.kernel_autoreverted"]
    assert summary.store_content_hash == _GOLDEN_CONTENT_HASH
    # The run identity is untouched by the flag (masks are inputs to the
    # blake3 formula, never re-encoded — the #1722 standing law).
    assert mask_run_id(mask=0b101, params=_PARAMS, **_CTX) == _GOLDEN_RUN_ID
