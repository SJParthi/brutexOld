# ADR-0021 — Historical-feed interface + feed-tagged lake root

**Decision IDs:** `DEC-FEED-LAKE-ROOT-001` (MIGRATION.md entry #1755),
`DEC-FEED-INTERFACE-001` (MIGRATION.md entry #1779)
**Date:** 2026-07-12
**Status:** **ACCEPTED** — shipped as PR-2 on `claude/feed-historical-interface`
(off origin/main `4865dad9`), the interface + feed-tagged-lake half of the
second-historical-feed program. PR-1 (a separate branch) owns the second
vendor's authenticated client + the vendor-token CI-guard retirement; PR-3 wires
the spine to consume the binding.
**Author:** PR-2 build.
**Builds on:** the canonical partition law (`brutex/data/partition.py`,
`FR-CAT-001`), the `MarketAdapter.fetch_ohlcv` fetch waist
(`brutex/adapters/protocol.py`), the keep-first merge-on-write dedup
(`DEC-LAKE-WRITE-UNIQUE-001`), and the COMMON orchestrator hash chain
(`brutex/orchestrator/audit.py`, Golden Rule #8).
**Named follow-ups minted here:** `DEC-FEED-SPINE-GENERALIZE-001` (the ingest
spine consumes the binding — PR-3, against the real second consumer),
`DEC-FEED-TREE-OBSERVABILITY-001` (teach every lake reader / heal / census /
backup-verify the `feeds/` subtree via `list_feed_roots`),
`DEC-FEED-DIVERGENCE-DIFF-001` (an observe-only cross-feed value-divergence diff
tool).

---

## 1. Context

Track 1 stores one vendor's (Groww's) historical bars in the canonical data lake
(`<lake_root>/bars/<exchange>/<segment>/<symbol>/<interval>/<yyyy>/<mm>.parquet`).
Adding a second historical feed requires two things that must NOT disturb the
existing feed's stored bytes or the frozen engine identity:

1. **Feed tagging** — a way to store a second feed's bars so the two feeds are
   never confused, never share a partition file, and the existing single-feed
   lake needs zero migration.
2. **A COMMON interface** — a way to describe a feed (its adapter, error policy,
   pacing, window-tiling, ledger namespaces, history floor) so the spine can be
   feed-parametric later without duplicating the `MarketAdapter` fetch waist.

The sacred invariant is **Groww byte-identity**: the existing write path, the
stored Parquet bytes, and the frozen engine goldens (`_GOLDEN_CONTENT_HASH` /
`_GOLDEN_RUN_ID`) must reproduce exactly.

---

## 2. Decision 1 — feed tagging by FEED-ROOT NESTING, resolved by an effective-root law

A non-default feed's entire lake nests under `<lake_root>/feeds/<feed_id>/` and
the EXISTING write stack runs verbatim on the resolved root — legal because every
component is already `lake_root`-parameterized (`ParquetWriter(*, lake_root)`,
`Catalog.open(path)`, `partition_write_lock(lake_root, dest)`, root-relative
manifests). `DEFAULT_FEED="groww"` resolves to the top root by **object
identity** (`feed_lake_root(root, feed=DEFAULT_FEED) is root`) — so the default
write path executes byte-ZERO new code. Groww byte-identity is therefore
STRUCTURAL: a feed sink built through the sanctioned constructor cannot reach the
top tree.

There is **no `feed` column** on `CandleBar` or the Parquet schema. Attribution
is `feed_of_partition_path` — path-structural, O(1), zero fs ops; an old
single-feed lake is attributed to the default feed with zero migration.

### Rejected alternatives (with evidence)

- **(B) a `feed` Parquet column.** Feeds would share a partition FILE while the
  merge-on-write dedup keys on `timestamp` alone (spot) / `(side, timestamp)`
  (options), keep-first (`DEC-LAKE-WRITE-UNIQUE-001`). A cross-feed
  same-minute bar would silently keep-first-COLLAPSE — **data loss**. Fixing it
  means re-keying `_merge_write`, the stats-manifest identity, `classify_bar_keys`,
  and the healer — the maximum blast radius across the byte-critical,
  100%-gated write stack.
- **(C) feed-in-segment.** Blocked by the `VALID_SEGMENTS` frozenset
  (`partition.py`). Widening it makes every sorted-walk reader SEE the new
  segment dirs and double-count.
- **(D) feed-in-symbol.** Leaks feed identity into every symbol-keyed join
  (completeness reconcile, the `lake_keys` hyphen-stripping fold, movers) —
  phantom rows and mis-joins.
- **Param-threading the writer/catalog/partition modules** (an alternate design)
  is rejected too: it edits three byte-critical 100%-gated modules for what
  root resolution gives free.

Dedup identity is **UNCHANGED**. A cross-feed same-instant bar is TWO retained
files, never a conflict, with zero cross-tree I/O. Per-feed roots additionally
give the second feed its own `catalog.duckdb`, `.manifest/`, `.locks/` — no DDL
migration, no shared-file contention. `parse_partition_path` is tail-anchored, so
`feeds/<f>/bars/...` already parses when scanners are later taught the subtree.

**HONEST limit:** `feeds/` is INVISIBLE to heal / census / display /
backup-verify until `DEC-FEED-TREE-OBSERVABILITY-001`. This is safe because zero
feed bytes can exist before PR-3 (PR-2 registers only the default feed).

---

## 3. Decision 2 — interface by `FeedBinding` COMPOSITION over `MarketAdapter`

`MarketAdapter.fetch_ohlcv(*, symbol, exchange, interval, start, end) ->
list[CandleBar]` stays THE fetch waist — duplicating it into a rival fetch
Protocol violates Golden Rule #11. What a feed additionally needs is the *wiring*
the spine currently couples to one vendor: the retry / rate-limit / terminal
error triple, the per-interval window-tiling policy, the rate ceilings, the
self-pacing predicates, the ledger-market namespaces, the history floor, and the
effective-root law. `FeedBinding` is that record (`brutex/feeds/binding.py`), with
`FeedErrorPolicy` and `FeedPacing` sub-records and grammar/law invariants in each
`__post_init__`.

All boundary types are plain data + `list[CandleBar]`, so a PyO3 Rust fetch/parse
core can satisfy `MarketAdapter` behind the SAME binding with zero interface
change (the control-plane fields here are resolved once per pull, so keeping them
in Python is defensible under the RUST-FIRST mandate).

`history_floor` is `date | None`: `None` means the vendor's earliest verified
1-minute date is UNVERIFIED and is never fabricated (Golden Rule #1). The default
feed carries the probe-verified `2020-01-01`; its backtesting `daily_quota` is
honestly `None`.

`build_feed_sink` is the ONE sanctioned sink constructor: it resolves the feed
root, builds `ParquetBarSink(ParquetWriter, Catalog)` over it, and wraps a
non-default feed in `FeedAuditSink` (Golden Rule #8 — a per-feed hash-chained
ingest log). The default feed returns the plain sink.

### Groww conformance — thin binding + object-identity pins

`build_groww_binding()` assembles the binding from the EXACT live spine objects
(`window_days_for_interval`, the `is_retryable`/`is_rate_limit`/`GrowwError`
triple, the `client_self_*` predicates, `GROWW_RATE_LIMIT_RPS_CEILING`/`_PER_MIN`,
the coverage-market namespaces, `EARLIEST_HISTORY`, an identity `lake_root_for`).
Tests pin each field by `is` identity, so the shim structurally cannot drift
without a red test, and `groww/ingest.py` / `run_ingest.py` show a **byte-zero
diff** (the strongest identity evidence). Spine consumption of the binding is the
named follow-up `DEC-FEED-SPINE-GENERALIZE-001` (PR-3, executed against the real
second consumer; seams: the error triple, `window_days_for`, the self-pacing
predicates, the `with_paced_client` gate).

The registry ships with ONLY the default feed, so PR-2 is vendor-token-free and
merges independent of PR-1. A second feed is registered later by a one-line data
change closing over PR-1's authenticated client; the `adapter_factory` kwargs
contract is pinned at PR-3 against PR-1's real client surface (not guessed now).

---

## 4. Consequences

- Groww output + stored bytes + engine identity are byte-identical. The
  cross-cutting parity harness (`tests/integration/test_feed_lake_byte_parity.py`)
  pins it: the default feed written through `build_feed_sink` produces parquet
  bytes byte-identical to the direct `ParquetBarSink`; adding a `feeds/feedx/`
  subtree in place leaves every top-root file byte-unchanged; and the frozen
  engine sweep golden (`_GOLDEN_CONTENT_HASH` / `_GOLDEN_RUN_ID`) reproduces with
  the feeds package imported — zero engine drift.
- A second feed can be added with a per-feed root, its own catalog/manifests, and
  a hash-chained ingest log — no shared-file contention, no schema migration.

### Reader-invisibility verdict (parity-harness evidence)

The feed subtree `feeds/<f>/` is a SIBLING of every anchor dir a reader walks
(`bars/` / `options/` / `.manifest/` / `sweeps/` / the catalog), and no existing
reader enumerates the lake root's direct children — so every reader is
**invisible-already**. The harness proves this empirically with the honest ±
methodology (ONE lake, snapshot each reader, add the feed subtree IN PLACE,
snapshot again — the only variable is the feed tree, never two independently
written lakes whose stat-token readers would legitimately differ by mtime):

| reader | verdict |
| --- | --- |
| `db_view.list_candle_datasets` (+ manifest-first) | invisible-already |
| `db_view.list_fno_candle_datasets` | invisible-already |
| `db_view.list_option_datasets` | invisible-already |
| `db_view.summarize_spot_datasets` (completeness) | invisible-already |
| `movers_snapshot.compute_spot_candidates` | invisible-already |
| `catalog_reconcile.reconcile_catalog_from_lake` | invisible-already |
| `lake_invariants.scan_lake_invariants` (census) | invisible-already |
| `lake_heal.lake_content_token` | invisible-already |
| `lake_heal.run_startup_sweep` (healer L3) | invisible-already |
| `lake_manifest` (top-root discovery manifest) | invisible-already |
| `lake_stats_manifest` (top-root stats manifest) | invisible-already |
| `lake_revision` (composite cache token) | invisible-already |

**Zero reader source edits were required.** Each reader is a separate pinned
assertion, so a future reader that starts descending into `feeds/` fails its own
test by name. Until `DEC-FEED-TREE-OBSERVABILITY-001` teaches heal/census/display
/backup-verify the subtree (via `list_feed_roots`), that invisibility is SAFE —
PR-2 registers only the default feed, so zero feed bytes can exist yet.
