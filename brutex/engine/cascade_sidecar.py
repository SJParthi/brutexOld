"""Deterministic Stage-4 cascade-verdict sidecar — persist the cascade outcome
alongside a sweep's results (DEC-CASCADE-WIRE-001).

The automatic path (:func:`brutex.engine.cascade_wire.run_cascade_for_survivors`
run on a sweep's top-K) produces a
:class:`~brutex.metrics.cascade.CascadeReport`; this module writes it to a
**sidecar Parquet** next to the per-tuple results so the verdict travels with the
data and the manual ``brutex cascade`` path can render the same table later. It is
the verdict twin of :mod:`brutex.engine.results_store`: a small, deterministic,
byte-reproducible sink — NOT a re-implementation of the cascade.

One row per candidate verdict, keyed by ``candidate_id`` (the BLAKE3 run-identity),
carrying the pass boolean, the rejection stage (or empty for survivors), the human
``detail`` string and the composite score (NULL for rejected candidates). Rows are
sorted by ``candidate_id`` before writing and the same determinism knobs the OHLC +
results lakes use (zstd-3, fixed row-group, ``statistics=False``, atomic replace)
are applied, so re-writing the same report is byte-identical (Golden Rule #4 / #7).

Honest envelope (Golden Rule #10): the sidecar holds ``O(K)`` verdict rows (the
cascade's input pool — the funnel's final ~K finalists), written once per sweep.
Not an O(1) hot path; a small offline write.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from brutex.models import BruteXError

if TYPE_CHECKING:
    from brutex.metrics.cascade import CascadeReport

_LOG = structlog.get_logger("brutex.engine.cascade_sidecar")

__all__ = [
    "CASCADE_SIDECAR_NAME",
    "CascadeSidecarError",
    "CascadeSidecarFlush",
    "cascade_verdict_table",
    "write_cascade_sidecar",
]

#: zstd level — deterministic across zstd >= 1.5 (mirrors results_store.ZSTD_LEVEL).
_ZSTD_LEVEL: Final[int] = 3
#: Fixed parquet row-group size — pinned so the byte stream is reproducible.
_ROW_GROUP_SIZE: Final[int] = 100_000

#: The conventional sidecar filename written beside a sweep's results root.
CASCADE_SIDECAR_NAME: Final[str] = "cascade_verdicts.parquet"


class CascadeSidecarError(BruteXError):
    """Raised when the cascade sidecar cannot be written (e.g. ``polars`` missing)."""


@dataclass(frozen=True, slots=True)
class CascadeSidecarFlush:
    """Outcome of a successful :func:`write_cascade_sidecar`.

    Attributes
    ----------
    path
        The sidecar Parquet file written.
    row_count
        Number of verdict rows (the cascade's ``n_input``).
    content_hash
        ``sha256`` hex of the file bytes — the content address (stable across
        re-writes of the same report, Golden Rule #4).
    bytes_written
        Size of the final Parquet file in bytes.
    """

    path: Path
    row_count: int
    content_hash: str
    bytes_written: int


def _render_bytes(report: CascadeReport) -> bytes:
    """Serialise a :class:`~brutex.metrics.cascade.CascadeReport` to deterministic Parquet bytes.

    Verdicts are sorted by ``candidate_id`` (so insertion order never changes the
    bytes), the polars schema is pinned, and the same reproducibility settings the
    OHLC lake uses are applied. ``rejected_at`` is the stage value or ``""`` for a
    survivor; ``composite_score`` is the survivor's score or ``None`` (Parquet null).
    """
    try:
        import polars as pl
    except ImportError as exc:  # pragma: no cover - exercised via write_cascade_sidecar
        raise CascadeSidecarError(
            "cascade sidecar requires 'polars' — install with 'pip install -e .[data]'"
        ) from exc

    ordered = sorted(report.verdicts, key=lambda v: v.candidate_id)
    columns: dict[str, list[object]] = {
        "candidate_id": [v.candidate_id for v in ordered],
        "passed": [v.passed for v in ordered],
        "rejected_at": ["" if v.rejected_at is None else v.rejected_at.value for v in ordered],
        "detail": [v.detail for v in ordered],
        "composite_score": [v.composite_score for v in ordered],
    }
    df = pl.DataFrame(
        columns,
        schema={
            "candidate_id": pl.Utf8,
            "passed": pl.Boolean,
            "rejected_at": pl.Utf8,
            "detail": pl.Utf8,
            "composite_score": pl.Float64,
        },
    )
    bio = io.BytesIO()
    df.write_parquet(
        bio,
        compression="zstd",
        compression_level=_ZSTD_LEVEL,
        row_group_size=_ROW_GROUP_SIZE,
        use_pyarrow=False,
        statistics=False,
    )
    return bio.getvalue()


def write_cascade_sidecar(report: CascadeReport, path: Path) -> CascadeSidecarFlush:
    """Write *report*'s per-candidate verdicts to the sidecar Parquet at *path*.

    Deterministic + idempotent: re-writing the same report yields a byte-identical
    file (Golden Rule #4 / #7). The write is atomic (tempfile + ``os.replace``), so
    a kill mid-write never leaves a half-written file.

    Parameters
    ----------
    report
        The cascade outcome to persist (its ``verdicts`` become the rows).
    path
        Destination Parquet file (parents created).

    Returns
    -------
    CascadeSidecarFlush
        The path, row count, content hash and byte size.

    Raises
    ------
    CascadeSidecarError
        If ``polars`` is not installed.
    """
    content = _render_bytes(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, content)
    flush = CascadeSidecarFlush(
        path=path,
        row_count=len(report.verdicts),
        content_hash=hashlib.sha256(content).hexdigest(),
        bytes_written=len(content),
    )
    _LOG.info(
        "cascade_sidecar.write",
        path=str(path),
        row_count=flush.row_count,
        content_hash=flush.content_hash,
        n_survived=report.n_survived,
    )
    return flush


def cascade_verdict_table(report: CascadeReport, *, top: int | None = None) -> str:
    """Render *report* as a plain-text verdict table (the manual-path display).

    One line per candidate in survivor-rank-then-input order: the survivors first
    (best composite first), then the rejected in input order, each showing the
    pass/reject stage + detail. A header line summarises ``n_survived / n_input``.

    Parameters
    ----------
    report
        The cascade outcome.
    top
        Optional cap on the number of rows shown (``None`` = all).

    Returns
    -------
    str
        The formatted table.
    """
    by_id = {v.candidate_id: v for v in report.verdicts}
    survivors = [by_id[cid] for cid in report.survivor_ids]
    rejected = [v for v in report.verdicts if not v.passed]
    ordered = survivors + rejected
    if top is not None:
        ordered = ordered[:top]

    lines = [
        f"Stage-4 cascade: {report.n_survived}/{report.n_input} survived",
        "-" * 72,
    ]
    for v in ordered:
        if v.passed:
            score = "-" if v.composite_score is None else f"{v.composite_score:+.4f}"
            lines.append(f"PASS  {v.candidate_id[:12]}  score={score}  {v.detail}")
        else:
            stage = "-" if v.rejected_at is None else v.rejected_at.value
            lines.append(f"REJECT[{stage}]  {v.candidate_id[:12]}  {v.detail}")
    return "\n".join(lines)


def _atomic_write(dest: Path, content: bytes) -> None:
    """Atomically write *content* to *dest* via tempfile + fsync + rename.

    Mirrors :func:`brutex.engine.results_store._atomic_write`: a kill mid-write
    never leaves a half-written Parquet file on disk.
    """
    fd, tmp_path = tempfile.mkstemp(
        dir=str(dest.parent), prefix=".brutex-cascade-", suffix=".parquet.tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, dest)
    except OSError:  # pragma: no cover - defensive, mirrors results_store
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_path)
        raise
