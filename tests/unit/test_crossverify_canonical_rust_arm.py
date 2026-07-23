"""Flag-gated Rust-arm wiring tests for ``canonical`` (DEC-CROSSVERIFY-RUST-CORE).

STEM-MATCHED to ``brutex/crossverify/canonical.py`` (the
``test_crossverify_canonical*`` convention) so the ci.yml per-module stanza
AND the auto-discovery coverage gate measure the module's NEW flag-ON
branches beside the UNMODIFIED ``test_crossverify_canonical.py`` mapping
suite. Fake crates only — the REAL-wheel arms live in the golden +
differential suites (the ops-rust-parity lane).
"""

from __future__ import annotations

import sys
import types
from datetime import date
from typing import TYPE_CHECKING, Any

import pytest
from brutex.crossverify import canonical, rust_seam
from brutex.crossverify.canonical import CanonicalIdentity, canonical_identity, s3_key

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


def _plant_spy_crate(monkeypatch: pytest.MonkeyPatch, received: list[tuple]) -> _FakeOpsModule:
    """A faithful fake crate recording every canonical FFI argument tuple."""
    module = _FakeOpsModule("brutex_ops")
    module.__version__ = "0.0-test"

    def identity(*args: Any) -> tuple[str, str, str, str, str, str] | None:
        received.append(tuple(args))
        exchange, segment, lake_symbol = args[0], args[1], args[2]
        return canonical._identity_fields(
            canonical._canonical_identity_python(
                exchange=exchange, segment=segment, lake_symbol=lake_symbol
            )
        )

    module.crossverify_render_csv = lambda header, rows: (b"", None)
    module.crossverify_canonical_identity = identity
    module.crossverify_s3_key = lambda prefix, day_iso, token, symbol: (
        f"{prefix}/{day_iso}/{token}/{symbol}.csv"
    )
    monkeypatch.setitem(sys.modules, "brutex_ops", module)
    return module


class TestCanonicalFlagOff:
    """Flag OFF runs the verbatim Python bodies in place — crate untouched."""

    def test_off_is_identical_and_crate_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The explicit "0" kill switch (retargeted from the pre-flip unset
        # arm — DEC-RUST-DEFAULT-ON-WAVE-001); assertions unchanged.
        monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, "0")
        monkeypatch.setattr(
            rust_seam,
            "_resolve_crate",
            lambda: pytest.fail("flag-OFF touched the crate resolver"),
        )
        assert canonical_identity(exchange="NSE", segment="CASH", lake_symbol="M&M") == (_IDENTITY)
        assert (
            s3_key(prefix="crossverify/groww", day=_DAY, identity=_IDENTITY)
            == "crossverify/groww/2026-07-10/NSE_EQ/M&M.csv"
        )


class TestCanonicalFlagOn:
    """The flag-ON arm: the Python parse's fields cross; deny-by-default."""

    def test_all_mapping_arms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, "1")
        received: list[tuple] = []
        _plant_spy_crate(monkeypatch, received)
        # CASH — the fno trio crosses as None/False.
        assert canonical_identity(exchange="NSE", segment="CASH", lake_symbol="M&M") == (_IDENTITY)
        assert received[-1] == ("NSE", "CASH", "M&M", None, None, False, None)
        # FNO FUT — the PYTHON-parsed grammar fields cross the FFI.
        fut = canonical_identity(exchange="NSE", segment="FNO", lake_symbol="NSE-NIFTY-30Jul26-FUT")
        assert fut is not None
        assert fut.canonical_symbol == "NIFTY-2026-07-30-FUT"
        assert received[-1] == (
            "NSE",
            "FNO",
            "NSE-NIFTY-30Jul26-FUT",
            "NIFTY",
            "2026-07-30",
            True,
            "NSE",
        )
        # A parsed OPTION leg crosses fno_is_future=False → None both arms.
        assert (
            canonical_identity(
                exchange="NSE", segment="FNO", lake_symbol="NSE-NIFTY-30Jul26-24000-CE"
            )
            is None
        )
        # An unparsed FNO dir crosses the None trio → None both arms.
        assert canonical_identity(exchange="NSE", segment="FNO", lake_symbol="JUNK") is None
        # Deny-by-default parity on the INDEX arm.
        assert canonical_identity(exchange="BSE", segment="INDEX", lake_symbol="BSEIT") is None

    def test_s3_key_flag_on_pre_formats_the_day(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, "1")
        _plant_spy_crate(monkeypatch, [])
        assert (
            s3_key(prefix="crossverify/groww", day=_DAY, identity=_IDENTITY)
            == "crossverify/groww/2026-07-10/NSE_EQ/M&M.csv"
        )

    def test_flag_on_latched_seam_serves_the_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, "1")
        _plant_spy_crate(monkeypatch, [])
        rust_seam._disable("test_latch")
        assert canonical_identity(exchange="NSE", segment="CASH", lake_symbol="M&M") == (_IDENTITY)
        assert (
            s3_key(prefix="crossverify/groww", day=_DAY, identity=_IDENTITY)
            == "crossverify/groww/2026-07-10/NSE_EQ/M&M.csv"
        )
