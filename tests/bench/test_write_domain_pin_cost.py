"""Bench pin for the Pin D1-1 per-write domain/masquerade cost (DEC-WRITE-DOMAIN-CALLER-BINDING-001).

The caller-binding commit put a guarded ``dest.resolve()`` + one ``lstat``
swap-rail probe on EVERY ``ParquetWriter.write()`` — the domain-CARRYING arm
runs :func:`~brutex.data.write_domain.assert_write_domain`, the profile-FREE
default arm runs the :func:`~brutex.data.write_domain.assert_profile_free_dest`
masquerade pin — including the highest-volume lane, the pipeline prebuild's
~10⁵ derived-partition writes. The hostile-review MINOR-1 asked for the cost
to be MEASURED, not hand-waved ("noise next to render/fsync/replace"). This
file is that measurement + its permanent regression gate, three pins:

1. **A/B arm parity** — the masquerade-pin arm (``write_domain=None``, the
   default every maintenance/prebuild lane rides) vs the domain-bound arm
   (``write_domain=primary_write_domain(...)``, the live-pull builders'
   shape): median fresh-partition write cost per arm, ratio bounded by the
   shared file-I/O :func:`ratio_ceiling` tier (< 8× shared CI / 1.1× M4-Pro
   strict) — the two arms differ only in WHICH O(1) assert runs.
2. **Pin cost ≪ write cost** — the masquerade assert itself is micro-timed
   beside the full write it guards; its median per-call cost must stay under
   25% of the median per-write total (measured ~3% — see below; the 25%
   bound is the never-dominates law with wide shared-CI headroom).
3. **O(1) in lake size** — the pin is one resolve + one ``lstat`` + one
   tuple-prefix compare on the DEST only; its per-call cost on a lake padded
   with 400 sibling partition dirs must stay flat vs a bare lake
   (:func:`ratio_ceiling` bounded — a regression that enumerated the lake
   would scale and blow the ratio).

MEASURED (2026-07-16, sandbox shared-CI-class runner, medians — the figures
the ``parquet_writer.py`` cost comment + MIGRATION.md #1834 now cite):
masquerade-pin arm 3568.8 µs/write vs domain-bound arm 3577.3 µs/write
(ratio 1.002× — arm-indistinguishable); the masquerade assert itself
114.5 µs/call ≈ 3.2% of the 3539.5 µs full fresh-partition write; pin cost
bare-lake 140.6 µs vs 400-sibling padded lake 143.4 µs (ratio 1.02× —
flat). Wall-clock numbers move with hardware; the GATES are the
ratios/fractions above, which hold on any tier.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median

import pytest

from tests.bench._o1_tiers import ratio_ceiling

# The CI ``bench`` job installs lean (no ``[data]`` extra), so ``polars`` —
# which ``ParquetWriter`` needs at write time — may be absent. Skip the whole
# module then (the ``test_parquet_write_o1.py`` idiom); the data-layer symbols
# are imported only after this guard passes.
pytest.importorskip("polars")

from brutex.data import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.write_domain import (
    assert_profile_free_dest,
    primary_write_domain,
    resolve_profile_free_root,
)

#: Warm-up writes discarded per arm before timing (page-cache/dentry settle).
_WARMUP = 5
#: Timed fresh-partition writes per arm (median taken).
_WRITES = 40
#: Micro-bench loop size per sample for the pin-only timing (µs-scale op).
_PIN_LOOP = 200
#: Pin-only timing samples (median of loop means).
_PIN_SAMPLES = 5
#: Sibling partition dirs padded into the "large" lake for the flatness pin.
_PAD_SIBLINGS = 400
#: The never-dominates bound: the pin may cost at most this fraction of one
#: full write (measured ~0.03 — see the module docstring).
_MAX_PIN_FRACTION = 0.25


def _bar() -> CandleBar:
    """One deterministic bar (a single (2024, 01) partition)."""
    return CandleBar(
        timestamp=datetime(2024, 1, 25, 9, 15, 0, tzinfo=UTC),
        open=Decimal("22000.00"),
        high=Decimal("22010.00"),
        low=Decimal("21990.00"),
        close=Decimal("22005.00"),
        volume=100,
        open_interest=None,
    )


def _write(writer: ParquetWriter, sym: int) -> None:
    """Write one bar to a UNIQUE symbol → a brand-new partition file (no merge)."""
    writer.write(
        exchange="NSE",
        segment="INDEX",
        symbol=f"SYM{sym:06d}",
        interval="1minute",
        bars=[_bar()],
    )


def _median_write_ns(writer: ParquetWriter) -> float:
    """Median per-write wall time (ns) over fresh-partition writes."""
    for i in range(_WARMUP):
        _write(writer, i)
    samples: list[float] = []
    for j in range(_WRITES):
        t0 = time.perf_counter_ns()
        _write(writer, _WARMUP + j)
        samples.append(float(time.perf_counter_ns() - t0))
    return median(samples)


def _median_pin_ns(dest: Path, resolved_root: Path) -> float:
    """Median per-call wall time (ns) of the masquerade assert on *dest*."""
    # Warm the dentry cache so the timed loops measure the steady-state cost
    # every write after the first pays (the honest per-write figure).
    for _ in range(_PIN_LOOP):
        assert_profile_free_dest(dest, resolved_root)
    means: list[float] = []
    for _ in range(_PIN_SAMPLES):
        t0 = time.perf_counter_ns()
        for _ in range(_PIN_LOOP):
            assert_profile_free_dest(dest, resolved_root)
        means.append((time.perf_counter_ns() - t0) / _PIN_LOOP)
    return median(means)


def _seeded_dest(lake: Path) -> Path:
    """Mint one REAL partition file under *lake* and return its path."""
    writer = ParquetWriter(lake_root=lake)
    _write(writer, 999_999)
    dest = lake / "bars" / "NSE" / "INDEX" / "SYM999999" / "1minute" / "2024" / "01.parquet"
    assert dest.is_file(), "seed write did not land where the pin expects"
    return dest


class TestWriteDomainPinCost:
    """The Pin D1-1 per-write assert cost is measured, bounded and flat."""

    def test_masquerade_arm_vs_domain_arm_ratio(self, tmp_path: Path) -> None:
        """Profile-free (masquerade) vs domain-bound write cost stays ~1× (< tier ceiling)."""
        free = ParquetWriter(lake_root=tmp_path / "free")
        bound_root = tmp_path / "bound"
        bound = ParquetWriter(lake_root=bound_root, write_domain=primary_write_domain(bound_root))
        free_ns = _median_write_ns(free)
        bound_ns = _median_write_ns(bound)
        ratio = max(free_ns, bound_ns) / min(free_ns, bound_ns)
        ceiling = ratio_ceiling(8.0)
        print(
            f"\nmasquerade arm {free_ns / 1e3:.1f} us/write vs domain arm "
            f"{bound_ns / 1e3:.1f} us/write -> ratio {ratio:.3f}x (ceiling {ceiling}x)"
        )
        assert ratio < ceiling, (
            f"domain/masquerade write-arm ratio {ratio:.2f}x >= {ceiling}x — one arm's "
            "per-write assert has regressed past the O(1) tier ceiling"
        )

    def test_pin_cost_small_fraction_of_write(self, tmp_path: Path) -> None:
        """The masquerade assert costs a small fraction of the write it guards."""
        lake = tmp_path / "lake"
        dest = _seeded_dest(lake)
        pin_ns = _median_pin_ns(dest, resolve_profile_free_root(lake))
        write_ns = _median_write_ns(ParquetWriter(lake_root=lake))
        fraction = pin_ns / write_ns
        print(
            f"\nmasquerade pin {pin_ns / 1e3:.1f} us/call vs full write "
            f"{write_ns / 1e3:.1f} us -> fraction {fraction:.4f} "
            f"(bound {_MAX_PIN_FRACTION})"
        )
        assert fraction < _MAX_PIN_FRACTION, (
            f"the per-write masquerade pin costs {fraction:.2%} of a full write "
            f"(>= {_MAX_PIN_FRACTION:.0%}) — it may never approach the "
            "render/fsync/replace cost it rides beside"
        )

    def test_pin_cost_flat_in_lake_size(self, tmp_path: Path) -> None:
        """Pin cost is O(1) in lake size (one dest resolve + lstat — never a lake walk)."""
        small = tmp_path / "small"
        large = tmp_path / "large"
        small_dest = _seeded_dest(small)
        large_dest = _seeded_dest(large)
        # Pad the large lake with sibling partition dirs the pin must ignore.
        for i in range(_PAD_SIBLINGS):
            sib = large / "bars" / "NSE" / "INDEX" / f"PAD{i:06d}" / "1minute" / "2024"
            sib.mkdir(parents=True)
            (sib / "01.parquet").touch()
        small_ns = _median_pin_ns(small_dest, resolve_profile_free_root(small))
        large_ns = _median_pin_ns(large_dest, resolve_profile_free_root(large))
        ratio = max(small_ns, large_ns) / min(small_ns, large_ns)
        ceiling = ratio_ceiling(8.0)
        print(
            f"\npin cost bare lake {small_ns / 1e3:.2f} us vs {_PAD_SIBLINGS}-sibling "
            f"lake {large_ns / 1e3:.2f} us -> ratio {ratio:.3f}x (ceiling {ceiling}x)"
        )
        assert ratio < ceiling, (
            f"masquerade-pin cost ratio {ratio:.2f}x >= {ceiling}x across lake sizes — "
            "the per-write assert is scaling with the lake (it must judge ONLY its dest)"
        )
