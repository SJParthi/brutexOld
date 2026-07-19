# brutex-refine — the Stage-3 refinement optimizer core (Rust)

`DEC-RUST-STAGE3-REFINE-001`: the WHOLE Stage-3 optimizer math — the
deterministic restarted CMA-ES over the normalized (SL, TP) plane, the
Mode-A grid bijection, and the bounded per-leaderboard top-K survivor
selection — in pure, dependency-free Rust, shipped DEFAULT-ON (the pycma
Python oracle is DELETED in the same lane; `BRUTEX_REFINE_RUST` falsy
refuses loudly — there is no fallback).

Greenfield ruling: no pycma bit-match is claimed. Correctness = the
convergence suite over standard objectives (sphere 2D/5D, Rosenbrock
2D/5D), the determinism / hard-budget / bounds / restart-schedule laws,
the selection differential vs the verbatim two-pass reference, and the
frozen goldens (Rust literals + the Python JSON fixture).

Build the wheel into the project venv:

    maturin develop --release   # inside rust/brutex-refine

Layout mirrors `rust/brutex-feed` (pure-Rust rlib core, PyO3 behind the
optional `python` feature, maturin mixed layout, build.rs source-digest
stamp per DEC-RUST-SOURCE-DIGEST-TWIN-001).
