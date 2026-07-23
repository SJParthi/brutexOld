"""Rust-vs-Python parquet byte-parity corpus (ADR-0020 PR-1 — the core gate).

DEC-RUST-DATALAKE-IO-ROADMAP-001 (MIGRATION.md #1796). Regenerates the
slice-8 spike's 5-case corpus (plus the review-round all-null-overlay case)
through the REAL
:meth:`ParquetWriter._render_bytes` and asserts the ``brutex_lake`` crate's
render is BYTE-IDENTICAL (sha256 AND raw bytes) on every case, plus a
whole-write-path ON/OFF parity through :meth:`ParquetWriter.write` to a real
temp lake. The Rust bytes are obtained through the PRODUCTION seam in
steady-state mode (burn-in budget 0 — the returned bytes are the crate's,
no oracle compare cushions the equality) so nothing here replicates the
frame construction: any drift in `_render_bytes`' schema branching is
automatically part of the tested surface. Every engaged render additionally
runs :func:`_assert_crate_rendered` (refuter round 1): the seam's warn-only
degrade paths (``render_error`` / ``render_result_refused``) serve ORACLE
bytes WITHOUT latching ``rust_render_disabled``, so without that witness a
crate raising on every render would green the gate comparing
python-to-python — the guard asserts the engaged-once INFO fired and zero
fallback reasons are in the warn memo, so green MEANS the crate rendered.

REQUIRES the compiled crate + version lockstep. Auto-SKIPS loudly when
either is missing — the dedicated ``.github/workflows/lake-rust-parity.yml``
lane (and a local ``pip install rust/brutex-lake``) is where this suite has
teeth; the main CI test job deliberately never installs the crate.

Whitebox: drives the private ``_render_bytes`` seam directly (the seam under
test — DEC-P0-017 convention), so ``reportPrivateUsage`` is silenced.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import hashlib
import random
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

brutex_lake = pytest.importorskip(
    "brutex_lake",
    reason=(
        "brutex-lake crate not installed — the byte-parity corpus runs in the "
        "lake-rust-parity.yml lane (or locally after `pip install rust/brutex-lake`)"
    ),
)
pl = pytest.importorskip("polars")

from brutex.data import lake_rust_render as lrr  # noqa: E402
from brutex.data.models import CandleBar, FnoGreeksBar  # noqa: E402
from brutex.data.parquet_writer import ParquetWriter  # noqa: E402

if brutex_lake.pinned_polars_version() != pl.__version__:
    pytest.skip(
        "version-guard mismatch: installed polars "
        f"{pl.__version__} != crate pin {brutex_lake.pinned_polars_version()} — "
        "the facade would refuse to engage (fail-closed by design); re-run after "
        "the lockstep ritual (docs/runbooks/lake-rust-render-lockstep.md)",
        allow_module_level=True,
    )

BASE_TS = datetime(2024, 1, 1, 3, 45, tzinfo=UTC)  # 09:15 IST in UTC


def _mk_bar(i: int, *, oi: int | None) -> CandleBar:
    """The spike corpus generator's bar shape, verbatim (seeded — deterministic)."""
    rng = random.Random(i)
    base = Decimal(rng.randrange(1_000_000, 2_500_000)) / 100  # 2dp rupees
    spread = Decimal(rng.randrange(0, 5000)) / 100
    o = base
    h = base + spread
    lo = base - (Decimal(rng.randrange(0, 5000)) / 100)
    c = lo + (h - lo) * Decimal(rng.randrange(0, 101)) / 100
    c = c.quantize(Decimal("0.01"))
    return CandleBar(
        timestamp=BASE_TS + timedelta(minutes=i),
        open=o,
        high=max(h, o, c),
        low=min(lo, o, c),
        close=c,
        volume=rng.randrange(0, 5_000_000),
        open_interest=oi,
    )


def _mk_greeks_bar(i: int) -> FnoGreeksBar:
    rng = random.Random(1_000_000 + i)
    b = _mk_bar(i, oi=rng.randrange(0, 10_000_000))
    null_row = i % 7 == 3  # some honestly-NULL greek rows
    return FnoGreeksBar(
        timestamp=b.timestamp,
        open=b.open,
        high=b.high,
        low=b.low,
        close=b.close,
        volume=b.volume,
        open_interest=b.open_interest,
        iv=None if null_row else rng.uniform(0.05, 0.9),
        delta=None if null_row else rng.uniform(-1, 1),
        gamma=None if null_row else rng.uniform(0, 0.01),
        theta=None if null_row else -rng.uniform(0, 50),
        vega=None if null_row else rng.uniform(0, 100),
        rho=None if null_row else rng.uniform(-10, 10),
        spot_at_bar=None if null_row else (Decimal(rng.randrange(1_000_000, 2_500_000)) / 100),
        t_years_used=None if null_row else rng.uniform(0.001, 0.2),
        rate_used=None if null_row else 0.065,
        greeks_provenance_id=None if null_row else rng.randrange(0, 2**31),
    )


def _mk_all_null_overlay_bar(i: int) -> FnoGreeksBar:
    """Whole-window solver-demotion shape (review-round fold): EVERY overlay
    column fully null, plus an all-null Int64 OI.

    A real production shape — a window whose every Greeks solve demoted →
    all overlay fields ``None`` — whose FULLY-null Float64/Int32 overlay
    pages are a distinct parquet byte surface the original Greeks case never
    witnessed (it nulls only ``i % 7 == 3`` rows). The all-null-OI SPOT
    shape was already pinned by ``c1``/``c2`` (wholly ``oi=None``).
    """
    b = _mk_bar(i, oi=None)
    return FnoGreeksBar(
        timestamp=b.timestamp,
        open=b.open,
        high=b.high,
        low=b.low,
        close=b.close,
        volume=b.volume,
        open_interest=None,
        iv=None,
        delta=None,
        gamma=None,
        theta=None,
        vega=None,
        rho=None,
        spot_at_bar=None,
        t_years_used=None,
        rate_used=None,
        greeks_provenance_id=None,
    )


# The spike's 5 corpus cases: small spot with null OI, 17-column Greeks with
# null rows, the exact 100_000 row-group boundary, boundary+1 (2 row groups),
# and a realistic 250k-row month (3 row groups) — plus the review-round
# all-null-overlay case (e).
CORPUS: dict[str, list[CandleBar]] = {
    "a_small_spot_10": [_mk_bar(i, oi=(None if i % 3 == 0 else 1000 + i)) for i in range(10)],
    "b_greeks_options_50": [_mk_greeks_bar(i) for i in range(50)],
    "c1_rowgroup_100000": [_mk_bar(i, oi=None) for i in range(100_000)],
    "c2_rowgroup_100001": [_mk_bar(i, oi=None) for i in range(100_001)],
    "d_realistic_250000": [_mk_bar(i, oi=(i * 7 % 900_000)) for i in range(250_000)],
    "e_greeks_all_null_overlay_50": [_mk_all_null_overlay_bar(i) for i in range(50)],
}


@pytest.fixture(autouse=True)
def _fresh_process_view() -> Iterator[None]:
    lrr.reset_lake_rust_render_for_tests()
    yield
    lrr.reset_lake_rust_render_for_tests()


def _python_bytes(bars: list[CandleBar], monkeypatch: pytest.MonkeyPatch) -> bytes:
    # Explicit "0" (kill switch), NOT delenv-to-unset — under
    # DEC-RUST-DEFAULT-ON-WAVE-001 an unset flag resolves default-ON, which
    # would make this the RUST arm (a vacuous Rust-vs-Rust byte compare).
    monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "0")
    return ParquetWriter._render_bytes(bars)


def _assert_crate_rendered() -> None:
    """The vacuous-green guard (refuter round 1): prove the CRATE served the bytes.

    ``rust_render_disabled()`` alone is BLIND to the warn-only degrade paths:
    ``render_error`` / ``render_result_refused`` return ORACLE bytes without
    latching, so a crate that raises a degrade-set error (or returns
    non-bytes) on every render would green a bare byte-equality assert by
    comparing python-to-python — exactly the crate-broken class this corpus
    exists to catch. The complete witness is three-legged:

    * no latch (``crate_unavailable`` / ``crate_metadata_error`` /
      ``polars_version_mismatch`` / ``crate_panic`` / burn-in autorevert all
      set it);
    * the engaged-once INFO fired — it is recorded ONLY after the crate
      returned genuine ``bytes`` (and the per-test reset clears the memo, so
      it attests THIS test's renders, not a prior test's);
    * NO fallback reason is in the warn memo — ``render_error`` and
      ``render_result_refused`` cover the two non-latching oracle serves
      (a multi-render test could otherwise engage on render 1 and silently
      degrade on render 2).
    """
    assert not lrr.rust_render_disabled(), "the Rust path latched OFF mid-corpus"
    assert lrr._ENGAGED_KEY in lrr._OBSERVABILITY_MEMO, (
        "lake_rust_render.engaged never fired — the crate never served a render "
        "(vacuous green: the bytes under comparison would both be the oracle's)"
    )
    fallbacks = lrr._OBSERVABILITY_MEMO - {lrr._ENGAGED_KEY}
    assert not fallbacks, f"oracle fallback fired mid-corpus: {sorted(fallbacks)}"


def _rust_bytes(bars: list[CandleBar], monkeypatch: pytest.MonkeyPatch) -> bytes:
    """Render through the PRODUCTION seam in steady-state (post-burn-in) mode.

    ``remaining=0`` means the returned bytes are the CRATE's output with no
    oracle compare in the path — and :func:`_assert_crate_rendered` proves
    the crate genuinely produced them (a degrade-to-oracle serve fails the
    test instead of greening vacuously), so the equality is a genuine
    cross-engine byte witness.
    """
    monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "1")
    lrr.reset_lake_rust_render_for_tests(remaining=0)
    rendered = ParquetWriter._render_bytes(bars)
    _assert_crate_rendered()
    return rendered


class TestRenderByteParity:
    @pytest.mark.parametrize("case", sorted(CORPUS))
    def test_corpus_case_bit_identical(
        self, case: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bars = CORPUS[case]
        py = _python_bytes(bars, monkeypatch)
        rust = _rust_bytes(bars, monkeypatch)
        py_sha = hashlib.sha256(py).hexdigest()
        rust_sha = hashlib.sha256(rust).hexdigest()
        verdict = "MATCH" if rust == py else "DIVERGE"
        with capsys.disabled():
            print(
                f"parity {case}: rows={len(bars)} py_sha256={py_sha[:16]} "
                f"rust_sha256={rust_sha[:16]} bytes={len(py)} verdict={verdict}"
            )
        assert rust_sha == py_sha
        assert rust == py  # raw bytes, not just the digest

    def test_burn_in_path_agrees_and_never_autoreverts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The engaged burn-in compare passes on real data (no autorevert)."""
        bars = CORPUS["a_small_spot_10"]
        expected = _python_bytes(bars, monkeypatch)
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "1")
        lrr.reset_lake_rust_render_for_tests()  # full burn-in budget
        rendered = ParquetWriter._render_bytes(bars)
        assert rendered == expected
        _assert_crate_rendered()

    def test_shape_gate_compares_first_greeks_shape_after_spot_burn_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The review-round shape gate, driven through the REAL crate.

        The count budget is spent entirely on the SPOT shape; the first-ever
        Greeks-overlay shape must still run the byte-compare against the
        live oracle — and agree (no autorevert) on the real encoder.
        """
        spot = CORPUS["a_small_spot_10"]
        greeks = CORPUS["b_greeks_options_50"]
        expected_greeks = _python_bytes(greeks, monkeypatch)
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "1")
        lrr.reset_lake_rust_render_for_tests(remaining=1)  # budget spent on spot
        ParquetWriter._render_bytes(spot)
        rendered = ParquetWriter._render_bytes(greeks)  # first-ever Greeks shape
        assert rendered == expected_greeks
        _assert_crate_rendered()


class TestWholePathParity:
    def test_writer_write_on_off_byte_parity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ON/OFF whole-write-path parity through ``ParquetWriter.write``.

        Two independent temp lakes, identical bars; the flag-ON lake runs the
        crate in steady-state mode. The persisted partition files must be
        byte-identical and carry equal ``raw_hash``.
        """
        bars = [_mk_bar(i, oi=(None if i % 3 == 0 else 500 + i)) for i in range(1_000)]
        coords = {
            "exchange": "NSE",
            "segment": "INDEX",
            "symbol": "NSE-NIFTY",
            "interval": "1minute",
        }

        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "0")  # genuine oracle OFF arm
        off_lake = tmp_path / "off"
        off_result = ParquetWriter(lake_root=off_lake).write(bars=list(bars), **coords)

        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "1")
        lrr.reset_lake_rust_render_for_tests(remaining=0)  # steady state — crate bytes
        on_lake = tmp_path / "on"
        on_result = ParquetWriter(lake_root=on_lake).write(bars=list(bars), **coords)
        _assert_crate_rendered()

        off_files = sorted(p.relative_to(off_lake) for p in off_lake.rglob("*.parquet"))
        on_files = sorted(p.relative_to(on_lake) for p in on_lake.rglob("*.parquet"))
        assert off_files == on_files
        assert off_files, "the write produced no partition file"
        for rel in off_files:
            assert (on_lake / rel).read_bytes() == (off_lake / rel).read_bytes()
        assert on_result.raw_hash == off_result.raw_hash
        assert on_result.bars_added == off_result.bars_added
