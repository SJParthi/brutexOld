# fno-math — BruteX Rust hot kernel

PyO3-built Rust extension for BruteX's hot-path operations. Maturin-built
wheel imported as the Python module `fno_math`.

## Currently exposed

- `add_u64(a, b) -> int` — sanity-check primitive.
- `bitcount_u64s(data: list[int]) -> int` — total set bits across a list
  of `u64` values via `u64::count_ones`. Used by the future Bloom
  `unique_count_estimate` vectorisation and the Stage 1B AND-mask
  kernel.
- `__version__: str` — crate semver.

## Build

```
pip install maturin
maturin develop --release   # build + install into the active venv
# OR
maturin build --release     # produce a wheel in target/wheels/
```

## Status

- ✅ Maturin + PyO3 scaffold (DEC-P0-050, PR-K).
- 📋 Stage 1B AND-mask kernel (future PR).
- 📋 blake3 hash function (future PR — couples to combo_hash 9-field
  migration per DEC-P0-044).
- 📋 Bloom bit-density vectorisation (future PR).

See `docs/MIGRATION.md` for the full decision ledger.
