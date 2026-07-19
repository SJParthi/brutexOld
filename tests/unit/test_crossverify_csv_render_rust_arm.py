"""Flag-gated Rust-arm wiring tests for ``csv_render`` (DEC-CROSSVERIFY-RUST-CORE).

STEM-MATCHED to ``brutex/crossverify/csv_render.py`` (the
``test_crossverify_csv_render*`` convention) so the ci.yml per-module stanza
AND the auto-discovery coverage gate measure the module's NEW flag-ON
branches beside the UNMODIFIED ``test_crossverify_csv_render.py`` refusal/
byte suite. Fake crates only — the REAL-wheel arms live in the golden +
differential suites (the ops-rust-parity lane).
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from brutex.calendars.nse import TradingSession, get_session_checked
from brutex.crossverify import csv_render, rust_seam
from brutex.crossverify.canonical import CanonicalIdentity
from brutex.crossverify.csv_render import ExportContractError, ExportRow, render_day_csv

if TYPE_CHECKING:
    from collections.abc import Iterator

_DAY = date(2026, 7, 10)

_IDENTITY = CanonicalIdentity(
    exchange="NSE",
    lake_segment="CASH",
    lake_symbol="M&M",
    canonical_symbol="M&M",
    segment_token="NSE_EQ",
    instrument_class="cash_spot",
)


def _session() -> TradingSession:
    session = get_session_checked(_DAY)
    assert session is not None
    return session


def _rows(n: int = 2) -> list[ExportRow]:
    out: list[ExportRow] = []
    for minute in range(n):
        base = Decimal("2900.00") + Decimal(minute) * Decimal("0.05")
        out.append(
            ExportRow(
                ts_utc=datetime(2026, 7, 10, 3, 45 + minute, tzinfo=UTC),
                open=base,
                high=base + Decimal("0.30"),
                low=base - Decimal("0.25"),
                close=base + Decimal("0.10"),
                volume=100 + minute,
                oi=None,
            )
        )
    return out


def _oracle_bytes(n: int = 2) -> bytes:
    return csv_render._render_day_csv_python(identity=_IDENTITY, rows=_rows(n), session=_session())


@pytest.fixture(autouse=True)
def _fresh_seam() -> Iterator[None]:
    rust_seam.reset_crossverify_rust_for_tests()
    yield
    rust_seam.reset_crossverify_rust_for_tests()


class _FakeOpsModule(types.ModuleType):
    """A fake ``brutex_ops`` with DECLARED seam attributes (pyright-legal assigns)."""

    __version__: str
    crossverify_render_csv: Any
    crossverify_canonical_identity: Any
    crossverify_s3_key: Any


def _plant_faithful_crate(monkeypatch: pytest.MonkeyPatch) -> _FakeOpsModule:
    """A fake crate whose assembly agrees with the ``csv.writer`` byte law."""
    module = _FakeOpsModule("brutex_ops")
    module.__version__ = "0.0-test"

    def render(header: str, rows: list[list[str]]) -> tuple[bytes | None, tuple | None]:
        if not rows:
            return None, (1, 0, 0)
        lines = [header] + [",".join(cells) for cells in rows]
        return ("\n".join(lines) + "\n").encode("utf-8"), None

    module.crossverify_render_csv = render
    module.crossverify_canonical_identity = lambda *args: None
    module.crossverify_s3_key = lambda *args: ""
    monkeypatch.setitem(sys.modules, "brutex_ops", module)
    return module


class TestRenderDayCsvFlagOff:
    """Flag OFF runs the verbatim Python body in place — crate untouched."""

    def test_off_is_byte_identical_and_crate_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(rust_seam.CROSSVERIFY_RUST_ENV, raising=False)
        monkeypatch.setattr(
            rust_seam,
            "_resolve_crate",
            lambda: pytest.fail("flag-OFF touched the crate resolver"),
        )
        assert render_day_csv(identity=_IDENTITY, rows=_rows(), session=_session()) == (
            _oracle_bytes()
        )


class TestRenderDayCsvFlagOn:
    """The flag-ON arm: shared validation Python-side, assembly routed."""

    def test_flag_on_matches_oracle_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, "1")
        _plant_faithful_crate(monkeypatch)
        assert render_day_csv(identity=_IDENTITY, rows=_rows(), session=_session()) == (
            _oracle_bytes()
        )

    def test_flag_on_refusals_raise_the_exact_python_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every refusal fires from the SHARED Python loop BEFORE any FFI."""
        monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, "1")
        _plant_faithful_crate(monkeypatch)
        with pytest.raises(ExportContractError, match="M&M: empty row set"):
            render_day_csv(identity=_IDENTITY, rows=[], session=_session())
        rows = _rows(2)
        rows[1] = ExportRow(
            ts_utc=rows[0].ts_utc,  # duplicate minute
            open=rows[1].open,
            high=rows[1].high,
            low=rows[1].low,
            close=rows[1].close,
            volume=rows[1].volume,
            oi=rows[1].oi,
        )
        with pytest.raises(ExportContractError, match="rows not strictly increasing"):
            render_day_csv(identity=_IDENTITY, rows=rows, session=_session())

    def test_flag_on_latched_seam_serves_the_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, "1")
        _plant_faithful_crate(monkeypatch)
        rust_seam._disable("test_latch")
        assert render_day_csv(identity=_IDENTITY, rows=_rows(), session=_session()) == (
            _oracle_bytes()
        )
