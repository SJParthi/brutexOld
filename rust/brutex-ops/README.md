# brutex-ops — ops (backup) decision kernel

DEC-BACKUP-RUST-OPS-001. The pure DECISION plane of `brutex/backup/` in Rust:

* the dead-man verdict state machine (`deadman.py::evaluate_deadman`) over
  Python-precomputed `total_seconds()` deltas — primitives only across the FFI;
* the four golden-pinned launchd/cron template renderers
  (`schedule_render.py`) — `docs/runbooks/backup-templates/` is the
  cross-language byte pin (`tests/golden_templates.rs`);
* the restic `--json` output parsers + the fixed `restic backup` argv builder
  (`restic_runner.py`) — any abstain / raise-shaped outcome defers to the
  Python oracle (serde vs CPython `json` dialect divergences included).

Python keeps ALL I/O, subprocess, alerting, datetime parsing and every
logged/raised/alerted string; the default-OFF `BRUTEX_BACKUP_RUST` facade
(`brutex/backup/backup_rust.py`) routes with a per-law 16-call burn-in
compare + ship-the-oracle autorevert. The retained Python implementations
are the LIVE parity oracle — never deleted here.

Honest envelope (ADR-0020 banner, mandatory verbatim): **expected payoff ≈ 0 —
conversion is for uniformity, not speed.** Ships INERT; a default flip needs
its own DEC.

## Build

```sh
# pure-Rust core (no libpython linked)
cargo test
cargo clippy --all-targets -- -D warnings

# the wheel (project venv active)
maturin develop --release
```
