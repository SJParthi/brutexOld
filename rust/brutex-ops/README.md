# brutex-ops — the BruteX ops/automation Rust kernels (unified crate)

ONE crate, THREE ops lanes — hand-unified from the three divergent full
copies the lane PRs shipped (the `integration/brutex-ops-unified` vehicle):

| Lane | DEC | Kernels | Facade (default-OFF flag) |
| --- | --- | --- | --- |
| `backup` | DEC-BACKUP-RUST-OPS-001 (PR #1669) | dead-man verdict state machine · four golden-pinned launchd/cron template renderers · restic `--json` parse + argv laws | `brutex/backup/backup_rust.py` (`BRUTEX_BACKUP_RUST`) |
| `crossverify` | DEC-CROSSVERIFY-RUST-CORE-001 (PR #1673) | pinned CSV byte assembly · canonical-identity mapping + S3-key law | `brutex/crossverify/rust_seam.py` (`BRUTEX_CROSSVERIFY_RUST`) |
| `nightly` | DEC-NIGHTLY-RUST-CORE-001 (PR #1676) | R42 break/exit-code law · audit-reconcile tripwire · verdict/Telegram summary · launchd plist render + schedule arithmetic | `brutex/nightly/nightly_rust.py` (`BRUTEX_NIGHTLY_RUST`) |

Every lane keeps its verbatim Python bodies as the LIVE parity oracle, and
every kernel crosses primitives / pre-formatted strings only — no Decimal,
no datetime, no tz math, no exception text is ever re-implemented in Rust.

## Layout

* `src/lib.rs` — module registration + re-exports ONLY (append-only spine).
* `src/backup.rs` / `src/crossverify.rs` / `src/nightly.rs` — the pure-Rust
  per-lane cores (`cargo test` links no libpython).
* `src/python.rs` — the WHOLE PyO3 surface, feature-gated behind `python`
  (turned on by maturin), with one clearly-delimited pyfunction +
  registration block per lane. Future lanes APPEND their own block.
* `python/brutex_ops/` — the mixed-layout shim: `__init__.py` re-exports the
  compiled `_brutex_ops` submodule behind the stale-wheel handshake
  (`_EXPECTED_COMPILED_SYMBOLS`, 27 symbols; `EXPECTED_BUILD.json` mirrors
  it), `__init__.pyi` + `py.typed` for strict type-checking.
* `build.rs` — bakes the crate-source BLAKE3 digest (FRAMING v1,
  `rust/source_digest_core.rs`) as `SOURCE_DIGEST` / `__source_digest__`;
  the ONE scheme covers all three lanes' sources.

## Build + test

```sh
cargo test --locked                 # pure-Rust cores, zero Python symbols
cargo clippy --all-targets --locked -- -D warnings
maturin develop --release           # the wheel (project venv active)
```

CI: the required `rust-brutex-ops` cargo job in `.github/workflows/ci.yml`
gates the pure core per PR; the dedicated NON-required
`.github/workflows/ops-rust-parity.yml` lane builds the wheel behind a hard
wheel-health gate and runs all three lanes' real-wheel + flag-ON parity legs.

## Panic profile (unification ruling)

`[profile.release]` deliberately keeps the cargo DEFAULT unwind (NO
`panic = "abort"`): all three facades carry pyo3 panic belts that degrade a
kernel panic to the Python oracle, and `abort` would neutralise every one of
them (a SIGABRT mid-backup / mid-crossverify / mid-nightly). The kernels are
still written structurally panic-free — the belts are defense-in-depth. The
ruling is recorded in the three lanes' MIGRATION.md entries.

## Honest envelope (Golden Rule #10)

Expected payoff ≈ 0 — the conversion is for uniformity, not speed (the
ADR-0020 banner, mandatory verbatim): every law runs at once-per-backup /
once-per-export / once-per-nightly-tick frequency and the engaged path ADDS
an env probe + an FFI hop per call. All three seams ship INERT (default
OFF); a default flip needs its own DEC citing a fresh green ops-rust-parity
run.
