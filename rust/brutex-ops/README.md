# brutex-ops

BruteX ops/automation Rust kernels (DEC-NIGHTLY-RUST-CORE-001).

The first lane (`src/nightly.rs`) re-hosts the PURE decision kernels of the
nightly zero-click chain (`brutex/nightly/`, DEC-SWEEP-NIGHTLY-AUTOMATION-001)
as token-for-token mirrors:

* the R42 break law + exit-code law (`chain.py::first_breaker` /
  `exit_code_for`) and the T2 audit-reconcile tripwire
  (`chain.py::audit_reconcile_tripwire`);
* the verdict + Telegram summary body builder (`report.py::_verdict` /
  `summary_lines` / `_count_text`);
* the launchd plist render + `--at` parse + pmset-wake / since-fire calendar
  arithmetic (`schedule.py::render_launchd_plist` / `parse_at` /
  `pmset_wake_command` / `seconds_since_scheduled_fire`).

The Python consumer is the default-OFF facade
`brutex/nightly/nightly_rust.py` behind `BRUTEX_NIGHTLY_RUST`; the verbatim
Python bodies are RETAINED as the live parity oracle (burn-in bit-compare +
autorevert; every guard/degrade path serves the oracle). Flag OFF — the
default — is byte-identical and never imports this crate.

HONESTY (the ADR-0020 banner, applied to this lane): expected payoff ≈ 0 —
conversion is for uniformity under the operator Rust-everywhere mandate, not
speed. Every kernel is microsecond-scale in Python; the seams run once per
nightly run / install / status probe.

Build (mirrors the sibling crates):

```bash
pip install maturin
maturin build --release --manifest-path rust/brutex-ops/Cargo.toml
pip install target/wheels/brutex_ops-*.whl
```

`cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test --release`
must be green before any change ships (CLAUDE.md §4).

Append-only structure: `src/lib.rs` is module registration ONLY; sibling ops
lanes (the named DEC-NIGHTLY-RUST-CORE-002 backup/restore ordering-law
follow-up) append their own `mod <lane>;` + registration lines — keep-both
merges by design.
