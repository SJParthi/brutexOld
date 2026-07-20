"""The Pin D1-1 CALLER BINDING on ``ParquetWriter`` (DEC-WRITE-DOMAIN-CALLER-BINDING-001).

The writer-seam half of the binding commit: the optional ``write_domain``
constructor param consults :func:`brutex.data.write_domain.assert_write_domain`
at the minted dest BEFORE any directory/lock/byte is touched, and a
profile-FREE writer (``write_domain=None``) carries the MASQUERADE pin
(:func:`~brutex.data.write_domain.assert_profile_free_dest`) instead.

Three laws pinned here, each through the REAL writer path
(``ParquetBarSink`` → ``ParquetWriter.write``):

* A/B BYTE-IDENTITY — a domain-armed primary write renders byte-identical
  partition files to the pre-binding profile-free writer (the assert passes
  silently on every legitimate dest);
* the FAILS-BEFORE refusal — a mis-rooted writer (its ``lake_root`` inside a
  feed subtree) bound to the PRIMARY domain refuses typed with NOTHING
  touched on disk (this test FAILS on the pre-binding tree: the write
  silently lands in the feed vendor's tree);
* the MASQUERADE fails-before — a profile-free writer whose dest RESOLVES
  under ``feeds/`` (the swapped-``bars/`` shape) refuses typed (pre-binding:
  a silent write into the feed vendor's tree — NG4E-1's domain-less arm).

Whitebox: the cache-probe test touches the private
``_resolved_profile_free_root`` helper, so per the DEC-P0-017 convention the
file silences ``reportPrivateUsage`` at the top.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.write_domain import WriteDomainError, primary_write_domain

_FEED = "feeds/vendor2"


def _bar(minute: int, *, close: str = "101.25") -> CandleBar:
    return CandleBar(
        timestamp=datetime(2024, 1, 4, 4, minute, tzinfo=UTC),
        open=Decimal("100.10"),
        high=Decimal("102.35"),
        low=Decimal("99.95"),
        close=Decimal(close),
        volume=10,
        open_interest=None,
    )


def _sink_write(root: Path, *, domain_root: Path | None) -> Path:
    """Write two bars through the REAL sink→writer path; return the partition file."""
    writer = ParquetWriter(
        lake_root=root,
        write_domain=None if domain_root is None else primary_write_domain(domain_root),
    )
    sink = ParquetBarSink(writer=writer)
    result = sink.append(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[_bar(0), _bar(1)],
    )
    assert result.bars_appended == 2
    (write_result,) = result.write_results
    return write_result.path


class TestDomainArmedAbByteIdentity:
    """The A/B pin: the primary-domain assert is SILENT on every legitimate dest."""

    def test_primary_domain_write_is_byte_identical_to_profile_free(self, tmp_path: Path) -> None:
        armed_root = tmp_path / "armed"
        legacy_root = tmp_path / "legacy"
        for root in (armed_root, legacy_root):
            root.mkdir()
        armed_path = _sink_write(armed_root, domain_root=armed_root)
        legacy_path = _sink_write(legacy_root, domain_root=None)
        assert armed_path.relative_to(armed_root) == legacy_path.relative_to(legacy_root)
        assert armed_path.read_bytes() == legacy_path.read_bytes()

    def test_domain_armed_repeat_write_stays_idempotent(self, tmp_path: Path) -> None:
        """The keep-first idempotence law survives the binding (re-pull shape)."""
        root = tmp_path / "lake"
        root.mkdir()
        first = _sink_write(root, domain_root=root)
        before = first.read_bytes()
        second = _sink_write(root, domain_root=root)
        assert second == first
        assert first.read_bytes() == before


class TestMisRootedPrimaryWriteRefuses:
    """The fails-before arm: pre-binding this write silently landed under feeds/."""

    def test_feed_rooted_writer_bound_to_primary_domain_refuses_typed(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        (lake / _FEED).mkdir(parents=True)
        writer = ParquetWriter(
            lake_root=lake / _FEED,  # the mis-rooted construction (feed sink)
            write_domain=primary_write_domain(lake),
        )
        with pytest.raises(WriteDomainError, match="PRIMARY write domain"):
            writer.write(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                bars=[_bar(0)],
            )
        # NOTHING touched: the refusal fires before any directory/lock/byte.
        assert list((lake / _FEED).iterdir()) == []

    def test_refusal_fires_through_the_real_sink_path_too(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        (lake / _FEED).mkdir(parents=True)
        with pytest.raises(WriteDomainError, match="PRIMARY write domain"):
            _sink_write(lake / _FEED, domain_root=lake)


class TestProfileFreeMasqueradeThroughTheWriter:
    """NG4E-1's domain-less arm at the writer seam (deferral-register item (b))."""

    def test_bars_symlinked_into_feeds_refuses_for_profile_free_writer(
        self, tmp_path: Path
    ) -> None:
        """Pre-binding fails-before: a profile-free writer whose ``bars/`` is
        a symlink INTO a feed subtree silently wrote the feed vendor's tree;
        the masquerade pin refuses typed and the feed tree stays empty."""
        lake = tmp_path / "lake"
        (lake / _FEED / "bars").mkdir(parents=True)
        (lake / "bars").symlink_to(lake / _FEED / "bars", target_is_directory=True)
        writer = ParquetWriter(lake_root=lake)  # NO domain — profile-free
        with pytest.raises(WriteDomainError, match="MASQUERADE"):
            writer.write(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                bars=[_bar(0)],
            )
        assert list((lake / _FEED / "bars").iterdir()) == []

    def test_profile_free_root_is_resolved_once_then_cached(self, tmp_path: Path) -> None:
        """Two writes through ONE profile-free writer resolve the root once
        (the lazy cache — both branches of the helper exercised)."""
        root = tmp_path / "lake"
        root.mkdir()
        writer = ParquetWriter(lake_root=root)
        for minute in (0, 1):
            writer.write(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                bars=[_bar(minute)],
            )
        # Whitebox cache probe: the resolved root is pinned after first write.
        assert writer._resolved_profile_free_root() == root.resolve()
