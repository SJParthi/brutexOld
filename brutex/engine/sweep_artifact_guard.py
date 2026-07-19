"""Typed guarded loader for legacy / corrupt results artifacts (G14, P2 side).

An OLD-SCHEMA results Parquet — one written before a column landed (the
pre-u128 single ``mask_bits`` era, the pre-``deflated_sharpe`` era) — is valid
Parquet, so :meth:`brutex.engine.results_store.ResultsStore.load` reads it
cleanly with ``pl.read_parquet`` and then dies on the FIRST missing column
inside ``_row_from_record`` with a **raw** ``KeyError`` (empirically pinned:
``KeyError('deflated_sharpe')`` on the committed
``tests/fixtures/results_store_old_schema.parquet``). That raw escape is
exactly what 500s ``/results`` — the existing skip set there catches
``ResultsStoreError`` + polars/OSError but not a bare ``KeyError``
(``sweep_results_view.py`` ``skip_errors``).

:func:`guarded_load` maps the EMPIRICAL legacy/corrupt error set onto ONE typed
:class:`ArtifactLoadError` so a consumer can degrade honestly with a single
catch. The mapped set (design §2, verified against the fixture):

* ``OSError`` — I/O failure reading the artifact;
* ``polars.exceptions.PolarsError`` / ``pyarrow.lib.ArrowException`` — corrupt
  or foreign bytes surfacing raw (each caught only when its library is
  importable);
* ``KeyError`` **whose missing key names a known results-store Parquet
  column** — the legacy-schema shape (the column vocabulary is imported from
  :mod:`brutex.engine.results_store` itself — ONE source of truth, §11 COMMON).

NOT swallowed (Golden Rule #1 — a genuine code bug must stay loud):

* a ``KeyError`` on any NON-column key (a foreign consumer bug);
* every ``ValueError`` / other exception class;
* :class:`~brutex.engine.results_store.ResultsStoreError` — already typed and
  handled by every existing reader; wrapping it would hide the store's own
  richer message.

The 1-line P3 adapter (merge-integration): ``sweep_results_view`` wraps
``ResultsStore.load`` as ``guarded_load(ResultsStore.load, path)`` and adds
:class:`ArtifactLoadError` to its skip set. Cost: O(loader) — this module adds
one ``try/except`` frame, nothing else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog

from brutex.engine import results_store as _results_store
from brutex.models import BruteXError

if TYPE_CHECKING:  # pragma: no cover — typing-only imports
    from collections.abc import Callable
    from pathlib import Path

_LOG = structlog.get_logger(__name__)

#: Every column name a results-store Parquet has EVER carried under the modern
#: schema — the legacy-schema ``KeyError`` discriminator. Imported from the
#: store's own field tuples (never a copy — CLAUDE.md §11 COMMON): a
#: ``KeyError`` on one of these names is the missing-column legacy shape; a
#: ``KeyError`` on anything else is a foreign code bug and propagates.
STORE_COLUMN_NAMES: Final[frozenset[str]] = frozenset(
    (
        *_results_store._STRING_FIELDS,  # pyright: ignore[reportPrivateUsage]
        *_results_store._MASK_LIMB_FIELDS,  # pyright: ignore[reportPrivateUsage]
        *_results_store._MONEY_PAISA_FIELDS,  # pyright: ignore[reportPrivateUsage]
        *_results_store._COUNT_FIELDS,  # pyright: ignore[reportPrivateUsage]
        *_results_store._METRIC_FIELDS,  # pyright: ignore[reportPrivateUsage]
        *_results_store._NULLABLE_METRIC_FIELDS,  # pyright: ignore[reportPrivateUsage]
        *_results_store._PRICE_FIELDS,  # pyright: ignore[reportPrivateUsage]
    )
)


class ArtifactLoadError(BruteXError):
    """A results artifact is LEGACY-SCHEMA or CORRUPT — degrade honestly, never 500.

    Carries the failing *path* plus :attr:`degrade_payload`, the honest
    JSON-ready verdict a consumer surfaces instead of the row data (the
    failure-matrix "parse fail ⇒ honest ``unreadable`` verdict" law).

    Parameters
    ----------
    message
        The human-readable cause (prefixed ``legacy or corrupt results
        artifact:`` by :func:`guarded_load`).
    path
        The artifact that failed to load.
    """

    def __init__(self, message: str, *, path: Path) -> None:
        super().__init__(message)
        self.path = path

    @property
    def degrade_payload(self) -> dict[str, str]:
        """The honest degrade dict: ``{path, verdict: "unreadable", reason}``."""
        return {"path": str(self.path), "verdict": "unreadable", "reason": str(self)}


def _optional_parquet_errors() -> tuple[type[BaseException], ...]:
    """The raw parquet-reader error classes importable in THIS process.

    ``polars`` / ``pyarrow`` are optional extras — each base class joins the
    caught set only when its library imports, so :func:`guarded_load` works
    identically (minus that arm) on a wheel-free install. O(1) after the
    interpreter caches the modules.
    """
    errors: list[type[BaseException]] = []
    try:
        import polars as pl

        errors.append(pl.exceptions.PolarsError)
    except ImportError:
        # Wheel-free install — the polars arm simply drops out of the set
        # (exercised in tests via sys.modules poisoning).
        pass
    try:
        import pyarrow.lib

        errors.append(pyarrow.lib.ArrowException)
    except ImportError:
        # Same drop-out for a pyarrow-free install.
        pass
    return tuple(errors)


def _is_legacy_schema_keyerror(exc: KeyError) -> bool:
    """``True`` iff *exc*'s missing key names a known results-store column.

    The empirical old-schema shape: ``ResultsStore.load`` reads every modern
    column per row, so the first column a legacy file lacks raises
    ``KeyError('<column>')`` (pinned ``'deflated_sharpe'`` on the committed
    fixture). A ``KeyError`` on any other key is a foreign bug — not ours to
    swallow.
    """
    return len(exc.args) == 1 and exc.args[0] in STORE_COLUMN_NAMES


def guarded_load[T](loader: Callable[[Path], T], path: Path) -> T:
    """Run ``loader(path)`` mapping the empirical legacy/corrupt error set to one type.

    Parameters
    ----------
    loader
        The artifact loader (e.g. ``ResultsStore.load``).
    path
        The artifact path handed to *loader*.

    Returns
    -------
    T
        The loader's value, unchanged, on success.

    Raises
    ------
    ArtifactLoadError
        On the mapped legacy/corrupt set (module docstring) — message
        ``legacy or corrupt results artifact: <path>: <cause>``, one WARN
        ``sweep_artifact_guard.load_failed`` logged.
    Exception
        Any OTHER exception the loader raises propagates UNCHANGED — a
        genuine code bug (foreign ``ValueError`` / ``KeyError``) and the
        store's own typed ``ResultsStoreError`` are never swallowed.
    """
    parquet_errors = _optional_parquet_errors()
    try:
        return loader(path)
    except KeyError as exc:
        if not _is_legacy_schema_keyerror(exc):
            raise
        message = f"legacy or corrupt results artifact: {path}: missing column {exc.args[0]!r}"
        _LOG.warning("sweep_artifact_guard.load_failed", path=str(path), error=message)
        raise ArtifactLoadError(message, path=path) from exc
    except OSError as exc:
        message = f"legacy or corrupt results artifact: {path}: {exc}"
        _LOG.warning("sweep_artifact_guard.load_failed", path=str(path), error=message)
        raise ArtifactLoadError(message, path=path) from exc
    except parquet_errors as exc:
        message = f"legacy or corrupt results artifact: {path}: {exc}"
        _LOG.warning("sweep_artifact_guard.load_failed", path=str(path), error=message)
        raise ArtifactLoadError(message, path=path) from exc


__all__ = [
    "STORE_COLUMN_NAMES",
    "ArtifactLoadError",
    "guarded_load",
]
