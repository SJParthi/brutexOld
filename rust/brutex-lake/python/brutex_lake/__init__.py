"""brutex_lake — BruteX data-lake Rust render seam (PyO3 bridge).

DEC-RUST-DATALAKE-IO-ROADMAP-001 (MIGRATION.md #1891; the datalake
conversion roadmap ADR). The
compiled cdylib ships as the private submodule ``brutex_lake._brutex_lake``;
this ``__init__.py`` re-exports its symbols (the fno-math DEC-P0-055 mixed
layout) so callers write ``import brutex_lake;
brutex_lake.render_parquet_from_ipc(...)``.

The consumers are the seven default-OFF facades:
``brutex/data/lake_rust_render.py`` — version-guarded, burn-in-checked,
wired into the two lake writers' render seams
(``ParquetWriter._render_bytes`` — the bars profile, PR-1 — and
``option_parquet_writer._atomic_write_parquet`` — the options-defaults
profile, PR-2);
``brutex/data/lake_rust_locks.py`` (ADR-0020 PR-4,
DEC-LAKE-RUST-LOCKS-ATOMIC-001, MIGRATION.md #1912) — the partition-flock
attempt/release + the atomic-write commit (chmod→fsync→rename) behind
``BRUTEX_LAKE_RUST_LOCKS``;
``brutex/data/lake_rust_resample.py`` (ADR-0020 Wave B / G5,
DEC-LAKE-RUST-RESAMPLE-001) — the tf_resampler seam
(``resample_1m_ipc`` / ``resample_all_ipc``);
``brutex/data/lake_rust_stats.py`` (ADR-0020 Wave C G6+G7,
DEC-LAKE-RUST-STATS-MANIFEST-001) — the stats-shard canonical-JSON render
(``render_stats_shard_json``); and ``brutex/data/window_ledger_rust.py``
(ADR-0020 Wave C-3 / G8, DEC-LAKE-RUST-WINDOW-LEDGER-001) — the
window-ledger status + merge-key date laws (``classify_window_status`` /
``date_isoformat``); and ``brutex/data/lake_rust_ledger_render.py``
(ADR-0020 G10 CONVERT, DEC-LAKE-RUST-LEDGERSTORE-CONVERT-001, MIGRATION.md
#1918) — the window-ledger parquet render
(``render_ledger_parquet_from_ipc``; ROW-EQUIVALENCE standard, never bytes
— the #1917 spike's measured-infeasible verdict); and
``brutex/data/detectors_rust.py`` (SLICE T24 —
PR-1 laws + the PR-2 fourth law + the PR-3a audit pair,
DEC-DATA-RUST-INTEGRITY-DETECTORS-001, MIGRATION.md #1948) — the T24
detector laws (``raw_integrity_blake2b_hex`` / ``interior_barless_runs`` /
``classify_partial_window_verdict`` /
``classify_today_completeness_verdict`` / ``economics_lot_fold`` /
``master_integrity_fold``, behind six per-module flags).
Nothing else in ``brutex/`` imports this package, and
every facade treats an absent/stale install as "stay on the Python law"
(one loud WARN), so a missing wheel can never break a lake write, a derived
resample, a stats shard, or a ledger record.

Lockstep law (the crate's whole reason to exist): the linked polars must be
REV-pinned at the commit the installed Python wheel's release tag pointed at
(tags are movable, so the manifest pins the commit), or the rendered bytes
may silently differ. ``pinned_polars_version()`` is the crate's side of that
handshake; the facade refuses to engage when it differs from the installed
``polars.__version__``. Bump ritual:
``docs/runbooks/lake-rust-render-lockstep.md``.
"""

from __future__ import annotations

from brutex_lake._brutex_lake import (
    __version__,
    atomic_commit_replace,
    classify_partial_window_verdict,
    classify_today_completeness_verdict,
    classify_window_status,
    date_isoformat,
    economics_lot_fold,
    interior_barless_runs,
    linked_zstd_version,
    lock_release,
    lock_try_exclusive,
    master_integrity_fold,
    merge_decision_from_ipc,
    pinned_polars_version,
    raw_integrity_blake2b_hex,
    render_ledger_parquet_from_ipc,
    render_parquet_defaults_from_ipc,
    render_parquet_from_ipc,
    render_stats_shard_json,
    resample_1m_ipc,
    resample_all_ipc,
)

__all__ = [
    "__version__",
    "atomic_commit_replace",
    "classify_partial_window_verdict",
    "classify_today_completeness_verdict",
    "classify_window_status",
    "date_isoformat",
    "economics_lot_fold",
    "interior_barless_runs",
    "linked_zstd_version",
    "lock_release",
    "lock_try_exclusive",
    "master_integrity_fold",
    "merge_decision_from_ipc",
    "pinned_polars_version",
    "raw_integrity_blake2b_hex",
    "render_ledger_parquet_from_ipc",
    "render_parquet_defaults_from_ipc",
    "render_parquet_from_ipc",
    "render_stats_shard_json",
    "resample_1m_ipc",
    "resample_all_ipc",
]
