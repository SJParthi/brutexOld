"""``FeedBinding`` — the wiring record around the existing fetch waist.

DEC-FEED-INTERFACE-001 (MIGRATION.md #1782). A historical feed is defined by
COMPOSITION over the EXISTING
:class:`~brutex.adapters.protocol.MarketAdapter` — its
``fetch_ohlcv(*, symbol, exchange, interval, start, end) -> list[CandleBar]``
stays THE fetch waist (Golden Rule #11 — duplicating it into a rival fetch
Protocol is forbidden). What a feed additionally needs is the *wiring* the ingest
spine currently couples to one vendor: the retry/rate-limit error triple, the
per-interval window-tiling policy, the rate ceilings, the self-pacing predicates,
the honest history floor, the ledger-market namespaces, and the effective
lake-root law. :class:`FeedBinding` is that record; all boundary types are plain
data + ``list[CandleBar]``, so a PyO3 Rust fetch/parse core can satisfy
:class:`MarketAdapter` behind the SAME binding with zero interface change (the
control-plane fields here are resolved once per pull, Python-defensible under the
RUST-FIRST mandate; recorded in ADR-0021).

Spine consumption of the binding is the named follow-up
DEC-FEED-SPINE-GENERALIZE-001 (executed in a later PR against the real second
consumer); PR-2 only defines the record + the ONE sanctioned sink constructor
(:func:`build_feed_sink`) and proves the default feed conforms by an
identity-pinned thin binding (:mod:`brutex.feeds.groww_binding`).

See TRACK1_BACKTEST_SPEC.md §"Ingest spine" and ADR-0021.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from brutex.adapters.ingest_common import WindowDaysFor
from brutex.adapters.protocol import MarketAdapter
from brutex.data.catalog import Catalog
from brutex.data.feed_audit import FeedAuditSink
from brutex.data.feed_root import DEFAULT_FEED, validate_feed_id
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import BarSink, ParquetBarSink

#: The conventional filename of a catalog DB inside its (effective) lake root.
CATALOG_DB_NAME = "catalog.duckdb"


@dataclass(frozen=True, slots=True)
class FeedErrorPolicy:
    """The retry / rate-limit / terminal-error classification triple for a feed.

    Mirrors the ingest spine's coupling to a vendor's error module: *is_retryable*
    decides whether a fetch error is re-attempted, *is_rate_limit* decides whether
    it feeds the AIMD ladder, and *terminal_base* is the vendor's base error type
    used to distinguish a graceful per-symbol quarantine from a genuine code bug.

    *is_retryable* / *is_rate_limit* are typed ``Callable[..., bool]`` (not
    ``Callable[[BaseException], bool]``) deliberately: a vendor's real classifier
    is annotated over its OWN error base (e.g. ``(GrowwError) -> bool``), which is
    contravariantly incompatible with a ``BaseException`` parameter under
    ``mypy --strict`` — pinning the live object by identity matters more than a
    nominal wide-parameter annotation, and *terminal_base* is the real type gate.
    """

    is_retryable: Callable[..., bool]
    is_rate_limit: Callable[..., bool]
    terminal_base: type[BaseException]

    def __post_init__(self) -> None:
        if not callable(self.is_retryable):
            raise ValueError("FeedErrorPolicy.is_retryable must be callable")
        if not callable(self.is_rate_limit):
            raise ValueError("FeedErrorPolicy.is_rate_limit must be callable")
        if not (
            isinstance(self.terminal_base, type) and issubclass(self.terminal_base, BaseException)
        ):
            raise ValueError(
                "FeedErrorPolicy.terminal_base must be a BaseException subclass, "
                f"got {self.terminal_base!r}"
            )


@dataclass(frozen=True, slots=True)
class FeedPacing:
    """A feed's rate ceilings (the vendor limits the controller is injected with).

    *ceiling_rps* is the per-second admission ceiling (> 0, finite);
    *ceiling_per_min* is the optional rolling-minute HARD governor; *daily_quota*
    is the optional daily cap — ``None`` means honestly none/unknown (Golden Rule
    #1; the default feed carries ``None`` per its UNVERIFIED backtesting quota).
    """

    ceiling_rps: float
    ceiling_per_min: int | None
    daily_quota: int | None

    def __post_init__(self) -> None:
        if not (self.ceiling_rps > 0 and math.isfinite(self.ceiling_rps)):
            raise ValueError(
                f"FeedPacing.ceiling_rps must be positive and finite, got {self.ceiling_rps!r}"
            )
        if self.ceiling_per_min is not None and self.ceiling_per_min <= 0:
            raise ValueError(
                f"FeedPacing.ceiling_per_min must be positive or None, got {self.ceiling_per_min!r}"
            )
        if self.daily_quota is not None and self.daily_quota <= 0:
            raise ValueError(
                f"FeedPacing.daily_quota must be positive or None, got {self.daily_quota!r}"
            )


@dataclass(frozen=True, slots=True)
class FeedBinding:
    """The COMMON wiring record for one historical feed.

    ``feed_id`` is grammar-validated (:func:`~brutex.data.feed_root.validate_feed_id`);
    the three ledger-market namespaces follow the prefix law
    ``<feed_id>`` / ``<feed_id>-future`` / ``<feed_id>-option`` so a feed's window
    ledger rows can never collide with another's;
    :attr:`lake_root_for` resolves the feed's effective lake root
    (default: :func:`~brutex.data.feed_root.feed_lake_root` bound to *feed_id*).
    ``history_floor`` is ``None`` when the vendor's earliest verified 1-minute
    date is UNVERIFIED (Golden Rule #1 — never fabricated).
    """

    feed_id: str
    ledger_market: str
    ledger_market_future: str
    ledger_market_option: str
    adapter_factory: Callable[..., MarketAdapter]
    window_days_for: WindowDaysFor
    error_policy: FeedErrorPolicy
    pacing: FeedPacing
    client_self_paces: Callable[[object], bool]
    client_self_reports_rate_limits: Callable[[object], bool]
    client_self_reports_successes: Callable[[object], bool]
    history_floor: date | None
    lake_root_for: Callable[[Path], Path]

    def __post_init__(self) -> None:
        validate_feed_id(self.feed_id)
        if self.ledger_market != self.feed_id:
            raise ValueError(
                f"ledger_market must equal feed_id {self.feed_id!r}, got {self.ledger_market!r}"
            )
        expected_future = f"{self.feed_id}-future"
        if self.ledger_market_future != expected_future:
            raise ValueError(
                f"ledger_market_future must be {expected_future!r}, got {self.ledger_market_future!r}"
            )
        expected_option = f"{self.feed_id}-option"
        if self.ledger_market_option != expected_option:
            raise ValueError(
                f"ledger_market_option must be {expected_option!r}, got {self.ledger_market_option!r}"
            )
        if not isinstance(self.error_policy, FeedErrorPolicy):
            raise ValueError(
                f"error_policy must be a FeedErrorPolicy, got {type(self.error_policy).__name__}"
            )
        if not isinstance(self.pacing, FeedPacing):
            raise ValueError(f"pacing must be a FeedPacing, got {type(self.pacing).__name__}")
        for name in (
            "adapter_factory",
            "window_days_for",
            "client_self_paces",
            "client_self_reports_rate_limits",
            "client_self_reports_successes",
            "lake_root_for",
        ):
            if not callable(getattr(self, name)):
                raise ValueError(f"FeedBinding.{name} must be callable")
        if not (self.history_floor is None or isinstance(self.history_floor, date)):
            raise ValueError(f"history_floor must be a date or None, got {self.history_floor!r}")


def build_feed_sink(
    *,
    lake_root: Path,
    binding: FeedBinding,
    code_commit: str | None = None,
) -> BarSink:
    """The ONE sanctioned constructor of a feed's write sink.

    Resolves the feed's effective lake root via ``binding.lake_root_for`` (the
    default feed → the top *lake_root* by identity; a non-default feed →
    ``<lake_root>/feeds/<feed_id>``), builds a :class:`ParquetBarSink` over a
    :class:`ParquetWriter` + :class:`Catalog` pointed at that root, and wraps it
    in a :class:`~brutex.data.feed_audit.FeedAuditSink` for a NON-default feed
    (Golden Rule #8). The default feed returns the plain sink — byte-zero new
    code on the default write path (DEC-FEED-LAKE-ROOT-001). A feed sink built
    through this constructor structurally cannot reach another feed's tree.
    """
    feed_root = binding.lake_root_for(lake_root)
    writer = ParquetWriter(lake_root=feed_root)
    catalog = Catalog.open(feed_root / CATALOG_DB_NAME)
    sink: BarSink = ParquetBarSink(writer=writer, catalog=catalog)
    if binding.feed_id == DEFAULT_FEED:
        return sink
    return FeedAuditSink(
        inner=sink,
        feed_id=binding.feed_id,
        feed_root=feed_root,
        code_commit=code_commit,
    )


__all__ = [
    "CATALOG_DB_NAME",
    "FeedBinding",
    "FeedErrorPolicy",
    "FeedPacing",
    "build_feed_sink",
]
