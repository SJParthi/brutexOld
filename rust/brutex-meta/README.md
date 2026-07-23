# brutex-meta

The decision-precision layer's O(1)/trade inference kernels
(DEC-META-LABEL-ANNOTATE-001 + DEC-ABSTAIN-GATE-STREAM-001 — BORN-RUST per
the operator's 2026-07-18 Rust-O(1) totality law: new decision-precision
code is implemented in Rust from day one, never Python-first).

Two pure kernels:

* `score_logistic` — the meta-label P(win) batch scorer: fixed-vector
  logistic inference (dot product + numerically-stable sigmoid) over a
  flattened row-major feature matrix. O(features)/trade = O(1)/trade for the
  fixed documented feature vector (`brutex/meta/features.py::FEATURE_NAMES`).
  Training (IRLS + CPCV out-of-fold discipline) stays offline Python — the
  sanctioned offline-statistics allowance; only inference is this hot path.
* `abstain_masks` — the abstention-first gate stream's per-element reason
  bitmasks (bit order = `brutex/meta/abstain.py::ABSTAIN_REASON_ORDER`).
  O(1)/element, branch-simple. ANNOTATION-ONLY today: the masks land in
  `.meta.json` sidecars, never in any simulation path.

Layout mirrors `rust/brutex-feed`: pure-Rust rlib core (cargo test links no
libpython), PyO3 behind the optional `python` feature, maturin mixed layout
with the stale-wheel handshake shim (`python/brutex_meta/__init__.py` +
`EXPECTED_BUILD.json`), build.rs source-digest stamp
(DEC-RUST-SOURCE-DIGEST-TWIN-001).

Consumed ONLY through the default-OFF facade `brutex/meta/meta_rust.py`
(`BRUTEX_META_GATE`, Pattern A opt-in; 16-call burn-in bit-compare vs the
retained Python oracles with ship-the-oracle autorevert). Deliberately NOT
in the Makefile turnkey build path (the brutex-lake posture — see
`tests/unit/test_run_builds_rust_kernels.py::OPTIONAL_KERNELS`); built by
its own CI job (`rust-brutex-meta` in `.github/workflows/ci.yml`).

Build the wheel locally:

```sh
cd rust/brutex-meta && maturin develop --release
```
