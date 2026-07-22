# brutex-feed — engine read-boundary decision kernel

`DEC-FEED-RUST-CORE-001`. A bit-identity-first Rust port of the DECISION
plane of `brutex/feed/replay.py::arrow_table_to_bars` + the
`brutex/data/bad_tick.py::BadTickScreen` state machine:

* engine-boundary price POSITIVITY refusal (DEC-STORED-DATA-VALIDATION-001 —
  field order open → high → low → close, first offender stops the scan);
* the in-session bad-tick keep/skip screen (DEC-SWEEP-DATA-EDGE-GUARDS-001 —
  the verbatim multiply-form f64 predicate, IST fixed +05:30 session
  grouping, first-bar-of-session exemption, baseline never advanced by a
  skip, one owed WARN per skipping session).

The kernel returns **integers only** (kept row indices, warn-event
coordinates, the positivity-stop coordinate). Python keeps ALL
materialization — `Decimal(str(float))`, `CandleBar` construction, error
f-strings, structlog WARNs — with the verbatim existing expressions over the
SAME `to_pylist()` objects, so the `bxsnap2` run-identity digest
(DEC-BXSNAP2-PARITY-GOLDEN-001) is byte-preserved by construction.

Consumed by `brutex/feed_rust.py` behind the default-OFF `BRUTEX_FEED_RUST`
flag; any degrade condition (wheel missing/stale, non-canonical schema,
nulls, out-of-envelope timestamps, kernel error/poison) falls back to the
byte-identical retained Python oracle.

Layout mirrors `rust/trading-core`: pure-Rust core (`cargo test` links no
libpython), PyO3 edge behind the optional `python` feature, maturin mixed
Python+Rust wheel (`python/brutex_feed/` shim with the stale-wheel
handshake), `build.rs` source-digest stamp (DEC-RUST-SOURCE-DIGEST-TWIN-001),
cargo-mutants scope in `.cargo/mutants.toml`, cargo-fuzz harness in `fuzz/`.

## Honest envelope (Golden Rule #10)

The scan is O(n_bars) per LAUNCH — a per-call feed-read latency win, not an
O(1) claim, and not on the ~33B-trial mask hot path.
