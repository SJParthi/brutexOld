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
from brutex.config.rust_strict import RUST_STRICT_ENV, RustFallbackRefused
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


@pytest.fixture
def strict_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the degrade-to-oracle contract to the mode that still HAS one.

    DEC-RUST-STRICT-NO-FALLBACK-001 ships ``BRUTEX_RUST_STRICT`` default-ON,
    where a Class-A (crate absent/stale/errored) or Class-B (crate answered
    but the RESULT is structurally refused) fall-back RAISES
    :class:`RustFallbackRefused` at the seam's choke point instead of serving
    the Python oracle.  A LATCHED seam is exactly that class.  The degrade
    machinery is not deleted — it stays reachable through the documented
    ``BRUTEX_RUST_STRICT=0`` kill switch, and THAT is the mode the tests
    requesting this fixture describe.  The strict-ON refusal for the SAME
    induced fault is pinned positively by
    :class:`TestStrictModeRefusesTheFallback`, so no coverage is traded away.

    Uses the SHARED ``monkeypatch`` (never a module-scoped
    ``MonkeyPatch.context()``) so set/unset ordering with the sibling env pins
    in this file stays deterministic.
    """
    monkeypatch.setenv(RUST_STRICT_ENV, "0")


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

    def test_flag_on_latched_seam_serves_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch, strict_off: None
    ) -> None:
        monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, "1")
        _plant_spy_crate(monkeypatch, [])
        rust_seam._disable("test_latch")
        assert canonical_identity(exchange="NSE", segment="CASH", lake_symbol="M&M") == (_IDENTITY)
        assert (
            s3_key(prefix="crossverify/groww", day=_DAY, identity=_IDENTITY)
            == "crossverify/groww/2026-07-10/NSE_EQ/M&M.csv"
        )


class TestStrictModeRefusesTheFallback:
    """The no-fallback guarantee, pinned POSITIVELY on this facade.

    DEC-RUST-STRICT-NO-FALLBACK-001.  ``BRUTEX_RUST_STRICT`` ships ON, so the
    latched-seam fall-back :class:`TestCanonicalFlagOn` describes under
    ``strict_off`` must RAISE here rather than quietly serving the Python
    oracle — otherwise that fixture would be a coverage trade instead of a
    relocation.

    Placement subtlety, read off the facade rather than assumed:
    :func:`rust_seam._disable` calls its strict gate BEFORE writing the latch
    (``rust_seam.py`` — "the refusal fires BEFORE the latch is written"), so
    under strict mode the latch is never set and every
    ``rust_seam_disabled()`` early-return in the routed laws is structurally
    unreachable.  Both halves are therefore pinned: the latch attempt itself
    refuses, and a fall-back induced through the REAL routed call (a burn-in
    mismatch) raises out of :func:`canonical_identity`.
    """

    def test_latching_the_seam_refuses_and_never_writes_the_latch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, "1")
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)  # shipped default = ON
        _plant_spy_crate(monkeypatch, [])
        with pytest.raises(RustFallbackRefused, match="test_latch"):
            rust_seam._disable("test_latch")
        assert not rust_seam.rust_seam_disabled(), (
            "the refusal fires BEFORE the latch write, so the latched "
            "early-return is unreachable under strict mode"
        )

    def test_burn_in_mismatch_raises_out_of_the_routed_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, "1")
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)  # shipped default = ON
        module = _plant_spy_crate(monkeypatch, [])
        # A DRIFTED mapping (wrong canonical_symbol): the burn-in bit-compare
        # would ship the ORACLE tuple and latch — strict mode refuses instead.
        module.crossverify_canonical_identity = lambda *args: (
            "NSE",
            "CASH",
            "M&M",
            "WRONG",
            "NSE_EQ",
            "cash_spot",
        )
        with pytest.raises(RustFallbackRefused, match="burn_in_mismatch_canonical"):
            canonical_identity(exchange="NSE", segment="CASH", lake_symbol="M&M")
