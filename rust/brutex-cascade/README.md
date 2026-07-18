# brutex-cascade

The cascade / results-depth compute laws of BruteX, in Rust
(DEC-RUST-CASCADE-RESULTS-001 — the T14 lane of the pure-Rust O(1)
conversion program).

Phase A (this crate version) hosts three law families, ported
token-for-token from the deleted Python implementations and verified
against the FROZEN pre-conversion golden corpus
(`tests/golden/fixtures/cascade_rust_parity/` in the main repo):

* **cascade_join** — the ONE cascade-report → display-join
  flattener/validator (`build_cascade_join`), including the structural
  fail-closed / per-item tolerant classification, duplicate-candidate
  exclusion, survivor-rank stamping and the capped defect-note law.
* **minute_path** — the 1-minute-path math (trade excursions, per-session
  minute-path equity series, independent paisa/pct max drawdown,
  IST session-date derivation).
* **combo_aggregates** — the per-combo aggregate fold (`aggregate_combo`)
  including nearest-rank percentiles, floor-mean integer stats, the
  min≤mean≤max-clamped float stats, the one-regime-wonder rule, exit
  tallies, and the CPython-`json.dumps(sort_keys=True, separators=(",",
  ":"), ensure_ascii=True)`-byte-identical lens JSON renders.

Support modules: `ist.rs` (fixed +05:30 IST calendar math over epoch-µs —
exact for post-1970 instants, matching `ZoneInfo("Asia/Kolkata")` on the
lake's 2020+ domain), `decimal.rs` (the `round_money` ROUND_HALF_UP 2-dp
rupee-string → int64-paisa law), `pyjson.rs` (the CPython
`py_encode_basestring_ascii` JSON encoder twin — the
`rust/brutex-lake/src/stats_shard.rs` precedent).

The Python-side faces live in `brutex/engine/{cascade_join,minute_path,
combo_aggregates}.py` (frozen dataclasses + boundary marshalling +
structlog observability) and resolve this wheel through
`brutex/engine/cascade_rust.py` (`BRUTEX_CASCADE_RUST` — default ON; an
explicit falsy value refuses loudly naming the DEC).

Build: `pip install ./rust/brutex-cascade` (maturin, abi3-py312).
Test: `cargo test --release --locked` (pure law cores — no Python needed).
