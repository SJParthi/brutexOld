"""The ``raw_integrity`` Rust-seam routing tests (SLICE T24 PR-1).

DEC-DATA-RUST-INTEGRITY-DETECTORS-001 (MIGRATION.md #1948). Stem-matched to
``brutex/data/raw_integrity.py`` so the discovery gate folds these into the
module's own 100% field: the flag-OFF inertness spy (the crate is NEVER
imported on the default path), the verbatim ``_hash_canonical_python``
oracle pin, the ON-arm parity through a fake crate (no wheel needed), the
burn-in-poison autorevert (the ORACLE digest ships), and the real-wheel
re-drives of the frozen golden + the hypothesis payload differential
(``importorskip``-gated).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

import brutex.data.detectors_rust as dr
import pytest
from brutex.data.raw_integrity import (
    _hash_canonical_python,
    build_record,
    canonicalize_payload,
    hash_payload,
    verify_record,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fakes.detectors_crate import fake_detectors_crate

#: The frozen-golden fixture — byte-identical to ``test_raw_integrity.py``'s
#: ``_PAYLOAD`` (the algorithm contract pin, re-driven flag-ON below).
_PAYLOAD: dict[str, object] = {
    "candles": [
        ["2024-01-01T09:15:00", 100.0, 110.0, 90.0, 105.0, 777, 42],
        ["2024-01-01T09:16:00", 105.0, 112.0, 104.0, 111.0, 800, 43],
    ]
}

#: The frozen golden digest (``test_hash_golden_frozen_value``'s contract).
_GOLDEN = "6efde60fa62320bf1b15eda6b132ac27b7f687f3ff731b17b2e204426b8e38c5"


@pytest.fixture(autouse=True)
def _strict_off_degrade_lattice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Degrade-lattice suite — pin ``BRUTEX_RUST_STRICT=0``.

    DEC-RUST-STRICT-NO-FALLBACK-001 ships strict mode ON by default, so a
    crate that is absent / stale / errored / burn-in-poisoned now RAISES
    ``RustFallbackRefused`` at the facade choke point instead of quietly
    serving the Python oracle.  The tests below exist precisely to exercise
    that oracle machinery — the still-supported ``BRUTEX_RUST_STRICT=0``
    mode — so the mode is DECLARED once, here, rather than by weakening any
    assertion.  Strict-ON refusal is pinned in each facade's own
    ``TestStrictRefusal`` class.

    Uses the SAME shared ``monkeypatch`` object as the sibling autouse
    fixtures (never its own ``MonkeyPatch.context()``): one object means one
    LIFO undo stack, so the value cannot leak past this module.
    """
    from brutex.config import rust_strict

    monkeypatch.setenv(rust_strict.RUST_STRICT_ENV, "0")


@pytest.fixture(autouse=True)
def _isolated_facade(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(dr.LAKE_RUST_RAW_INTEGRITY_ENV, raising=False)
    dr.reset_detectors_rust_for_tests()
    yield
    dr.reset_detectors_rust_for_tests()


def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(dr.LAKE_RUST_RAW_INTEGRITY_ENV, "1")


def _install_crate(monkeypatch: pytest.MonkeyPatch, crate: object) -> None:
    def _import(name: str) -> object:
        assert name == "brutex_lake"
        return crate

    monkeypatch.setattr(dr.importlib, "import_module", _import)


class TestFlagOffInertness:
    def test_off_never_imports_crate_and_matches_golden(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # OFF via the explicit "0" kill switch (default flipped ON — DEC-RUST-DEFAULT-ON-WAVE-001).
        monkeypatch.setenv(dr.LAKE_RUST_RAW_INTEGRITY_ENV, "0")

        def _bomb(name: str) -> object:
            raise AssertionError(f"flag-OFF hash_payload imported {name!r}")

        monkeypatch.setattr(dr.importlib, "import_module", _bomb)
        assert hash_payload(_PAYLOAD) == _GOLDEN

    def test_oracle_is_the_verbatim_digest_law(self) -> None:
        canonical = canonicalize_payload(_PAYLOAD)
        assert _hash_canonical_python(canonical) == _GOLDEN
        assert _hash_canonical_python(canonical) == (
            hashlib.blake2b(canonical, digest_size=32).hexdigest()
        )


class TestFlagOnFakeCrate:
    def test_on_arm_matches_golden_and_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate())
        assert hash_payload(_PAYLOAD) == _GOLDEN
        assert hash_payload({}) == _hash_canonical_python(b"[]")

    def test_burn_in_poison_ships_the_oracle_digest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(
            monkeypatch,
            fake_detectors_crate(blake2b_hex=lambda data, digest_size: "f" * 64),
        )
        # The poisoned crate can never corrupt a recorded proof: burn-in
        # ships the ORACLE digest + autoreverts the process.
        assert hash_payload(_PAYLOAD) == _GOLDEN
        assert dr.rust_detectors_disabled() is True

    def test_build_and_verify_record_ride_the_routed_digest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, fake_detectors_crate())
        record = build_record(
            _PAYLOAD,
            exchange="NSE",
            segment="CASH",
            groww_symbol="NSE-NIFTY",
            interval="1minute",
            window_start="2024-01-01 00:00:00",
            window_end="2024-01-31 00:00:00",
        )
        assert record.payload_hash == _GOLDEN
        assert verify_record(record, _PAYLOAD).matches is True
        tampered = {"candles": [["2024-01-01T09:15:00", 100.0, 110.0, 90.0, 105.01, 777, 42]]}
        assert verify_record(record, tampered).matches is False


class TestRealWheel:
    """Flag-ON re-drives against the REAL built wheel (skipped when absent)."""

    @staticmethod
    def _require_wheel(monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("brutex_lake")
        monkeypatch.setenv(dr.LAKE_RUST_RAW_INTEGRITY_ENV, "1")
        dr.reset_detectors_rust_for_tests(remaining=0)  # steady state

    def test_frozen_golden_reproduces_flag_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._require_wheel(monkeypatch)
        assert hash_payload(_PAYLOAD) == _GOLDEN

    @settings(max_examples=150, deadline=None)
    @given(
        rows=st.lists(
            st.lists(
                st.one_of(
                    st.text(max_size=12),
                    st.floats(allow_nan=False, allow_infinity=False),
                    st.integers(min_value=-(10**12), max_value=10**12),
                    st.none(),
                ),
                min_size=1,
                max_size=7,
            ),
            max_size=8,
        )
    )
    def test_payload_differential_vs_flag_off(self, rows: list[list[object]]) -> None:
        payload = {"candles": rows}
        with pytest.MonkeyPatch.context() as mp:
            pytest.importorskip("brutex_lake")
            mp.setenv(dr.LAKE_RUST_RAW_INTEGRITY_ENV, "1")
            dr.reset_detectors_rust_for_tests(remaining=0)
            routed = hash_payload(payload)
            mp.setenv(dr.LAKE_RUST_RAW_INTEGRITY_ENV, "0")  # kill switch → Python oracle recompute
            assert routed == hash_payload(payload)
        dr.reset_detectors_rust_for_tests()
