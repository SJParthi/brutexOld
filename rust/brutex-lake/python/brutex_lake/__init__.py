"""brutex_lake — BruteX data-lake Rust render seam (PyO3 bridge).

DEC-RUST-DATALAKE-IO-ROADMAP-001 (MIGRATION.md #1726; the datalake
conversion roadmap ADR). The
compiled cdylib ships as the private submodule ``brutex_lake._brutex_lake``;
this ``__init__.py`` re-exports its symbols (the fno-math DEC-P0-055 mixed
layout) so callers write ``import brutex_lake;
brutex_lake.render_parquet_from_ipc(...)``.

The consumer is ``brutex/data/lake_rust_render.py`` — the default-OFF,
version-guarded, burn-in-checked facade wired into the two lake writers'
render seams (``ParquetWriter._render_bytes`` — the bars profile, PR-1 —
and ``option_parquet_writer._atomic_write_parquet`` — the options-defaults
profile, PR-2). Nothing else in ``brutex/`` imports this
package, and the facade treats an absent/stale install as "stay on the
Python render" (one loud WARN), so a missing wheel can never break a lake
write.

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
    linked_zstd_version,
    pinned_polars_version,
    render_parquet_defaults_from_ipc,
    render_parquet_from_ipc,
)

__all__ = [
    "__version__",
    "linked_zstd_version",
    "pinned_polars_version",
    "render_parquet_defaults_from_ipc",
    "render_parquet_from_ipc",
]
