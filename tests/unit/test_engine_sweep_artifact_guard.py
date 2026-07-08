"""Unit tests — :mod:`brutex.engine.sweep_artifact_guard` (G14, DEC-SWEEP-UNKILLABLE-CORE-001).

The EMPIRICAL old-schema error is pinned against the COMMITTED fixture
``tests/fixtures/results_store_old_schema.parquet`` (a REAL pre-u128-limb /
pre-``deflated_sharpe`` results Parquet, generated with the old-shape schema:
single Int64 ``mask_bits`` column, every modern base column except the two limb
columns + ``deflated_sharpe``): ``ResultsStore.load`` reads it cleanly with
polars then raises a RAW ``KeyError('deflated_sharpe')`` from
``_row_from_record`` — the exact escape that 500s ``/results`` today.
:func:`guarded_load` must map that (and corrupt-bytes / IO errors) to the typed
:class:`ArtifactLoadError` while NEVER swallowing foreign errors.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

import polars as pl
import pyarrow.lib
import pytest
from brutex.engine import sweep_artifact_guard
from brutex.engine.results_store import (
    ResultsStore,
    ResultsStoreError,
    ResultsStoreSchemaError,
)
from brutex.engine.sweep_artifact_guard import (
    STORE_COLUMN_NAMES,
    ArtifactLoadError,
    guarded_load,
)
from structlog.testing import capture_logs

#: D-6 traceability — test name → requirement/gap tokens.
TRACEABILITY: dict[str, tuple[str, ...]] = {
    "test_g14_old_schema_fixture_raises_typed_schema_error_unguarded": ("G14",),
    "test_g14_raw_legacy_keyerror_maps_to_artifact_load_error": ("G14",),
    "test_g14_old_schema_fixture_through_guarded_load_passes_store_typed_error": ("G14",),
    "test_g14_degrade_payload_is_honest_unreadable_verdict": ("G14",),
    "test_g14_foreign_keyerror_not_swallowed": ("G14",),
    "test_g14_multi_arg_keyerror_not_swallowed": ("G14",),
    "test_g14_foreign_valueerror_not_swallowed": ("G14",),
    "test_g14_results_store_error_passes_through_unwrapped": ("G14",),
    "test_g14_oserror_maps_to_artifact_load_error": ("G14",),
    "test_g14_polars_error_maps_to_artifact_load_error": ("G14",),
    "test_g14_pyarrow_error_maps_to_artifact_load_error": ("G14",),
    "test_g14_success_passes_the_value_through_unchanged": ("G14",),
    "test_g14_store_column_names_exact": ("G14",),
    "test_g14_optional_error_set_with_both_libraries": ("G14",),
    "test_g14_optional_error_set_wheel_free": ("G14",),
    "test_g14_load_failed_warn_logged_once": ("G14",),
}

#: The committed REAL old-schema fixture (see the module docstring).
FIXTURE: Final[Path] = (
    Path(__file__).resolve().parents[1] / "fixtures" / "results_store_old_schema.parquet"
)

if not FIXTURE.is_file():
    # Fail LOUDLY at collection: the repo .gitignore carries a global
    # `*.parquet` rule, so this fixture must be committed with `git add -f`
    # (or a `!tests/fixtures/*.parquet` negation) — see wp-integration-notes.
    pytest.fail(
        f"committed old-schema fixture missing: {FIXTURE} — it is gitignore'd by the "
        "global '*.parquet' rule and must be force-added (git add -f) at commit time"
    )


def test_g14_old_schema_fixture_raises_typed_schema_error_unguarded() -> None:
    """The UNguarded production loader degrades the fixture as a TYPED error.

    ``ResultsStore.load`` now wraps the first missing REQUIRED column
    (``mask_bits_lo``) into a :class:`ResultsStoreSchemaError` naming the store
    path (DEC-SWEEP-RESULTS-DEPTH-001 ruling 5, the main fold), preserving the
    raw ``KeyError`` as ``__cause__``. This pins the fixture is genuinely
    old-schema (missing a required limb column, so it can never silently rot
    into something the modern loader accepts — which would hollow out every
    other test here) AND that its degrade is typed, never a raw escape.
    """
    with pytest.raises(ResultsStoreSchemaError) as excinfo:
        ResultsStore.load(FIXTURE)
    cause = excinfo.value.__cause__
    assert isinstance(cause, KeyError)
    assert cause.args == ("mask_bits_lo",)
    assert cause.args[0] in STORE_COLUMN_NAMES


def test_g14_raw_legacy_keyerror_maps_to_artifact_load_error(tmp_path: Path) -> None:
    """guarded_load maps a RAW legacy-schema KeyError (a store column) to ArtifactLoadError.

    A loader that lets a bare missing-column ``KeyError`` escape — an older or
    foreign loader that does NOT pre-wrap it the way ``ResultsStore.load`` now
    does — is degraded to the typed :class:`ArtifactLoadError` with the honest
    ``missing column`` message. This is the KeyError arm of :func:`guarded_load`
    exercised directly (the real fixture now degrades typed at the store
    boundary, so the raw-KeyError path is pinned via a synthetic loader).
    """
    artifact = tmp_path / "legacy.parquet"

    def raw_legacy_loader(_: Path) -> None:
        raise KeyError("mask_bits_lo")

    with pytest.raises(ArtifactLoadError) as excinfo:
        guarded_load(raw_legacy_loader, artifact)
    err = excinfo.value
    assert isinstance(err.__cause__, KeyError)
    assert err.__cause__.args == ("mask_bits_lo",)
    assert str(err) == (
        f"legacy or corrupt results artifact: {artifact}: missing column 'mask_bits_lo'"
    )
    assert err.path == artifact


def test_g14_old_schema_fixture_through_guarded_load_passes_store_typed_error() -> None:
    """guarded_load passes the store's OWN typed schema error through unwrapped.

    ``ResultsStore.load`` now types the old-schema failure itself
    (DEC-SWEEP-RESULTS-DEPTH-001 ruling 5), so wrapping it through
    :func:`guarded_load` yields the SAME :class:`ResultsStoreSchemaError` — a
    :class:`ResultsStoreError`, already handled by every resilient reader —
    never double-wrapped into an :class:`ArtifactLoadError` (module docstring:
    a typed ``ResultsStoreError`` is never swallowed).
    """
    with pytest.raises(ResultsStoreSchemaError):
        guarded_load(ResultsStore.load, FIXTURE)


def test_g14_degrade_payload_is_honest_unreadable_verdict() -> None:
    """The error carries the honest degrade payload a consumer can surface."""
    err = ArtifactLoadError("legacy or corrupt results artifact: x: boom", path=Path("/x"))
    assert err.degrade_payload == {
        "path": "/x",
        "verdict": "unreadable",
        "reason": "legacy or corrupt results artifact: x: boom",
    }


def test_g14_foreign_keyerror_not_swallowed(tmp_path: Path) -> None:
    """A KeyError on a NON-store-column key is a code bug — propagates unchanged."""

    def buggy(_: Path) -> None:
        raise KeyError("not_a_store_column")

    with pytest.raises(KeyError) as excinfo:
        guarded_load(buggy, tmp_path / "x.parquet")
    assert excinfo.value.args == ("not_a_store_column",)


def test_g14_multi_arg_keyerror_not_swallowed(tmp_path: Path) -> None:
    """A KeyError whose args are not the single-column shape propagates unchanged."""

    def buggy(_: Path) -> None:
        raise KeyError("deflated_sharpe", "extra")

    with pytest.raises(KeyError):
        guarded_load(buggy, tmp_path / "x.parquet")


def test_g14_foreign_valueerror_not_swallowed(tmp_path: Path) -> None:
    """A ValueError is never mapped — genuine bugs stay loud (Golden Rule #1)."""

    def buggy(_: Path) -> None:
        raise ValueError("genuine bug")

    with pytest.raises(ValueError, match="genuine bug"):
        guarded_load(buggy, tmp_path / "x.parquet")


def test_g14_results_store_error_passes_through_unwrapped(tmp_path: Path) -> None:
    """The store's OWN typed error (corrupt bytes) passes through unwrapped.

    Driven END-TO-END through the REAL loader on a genuinely corrupt file:
    ``ResultsStore.load`` wraps the polars parse failure itself, and
    guarded_load must not double-wrap an already-typed, already-handled error.
    """
    corrupt = tmp_path / "corrupt.parquet"
    corrupt.write_bytes(b"\x00\x01 not parquet at all")
    with pytest.raises(ResultsStoreError):
        guarded_load(ResultsStore.load, corrupt)


def test_g14_oserror_maps_to_artifact_load_error(tmp_path: Path) -> None:
    def failing(_: Path) -> None:
        raise OSError(5, "Input/output error")

    with pytest.raises(ArtifactLoadError) as excinfo:
        guarded_load(failing, tmp_path / "x.parquet")
    assert isinstance(excinfo.value.__cause__, OSError)
    assert str(excinfo.value).startswith("legacy or corrupt results artifact: ")


def test_g14_polars_error_maps_to_artifact_load_error(tmp_path: Path) -> None:
    """A RAW polars error escaping a loader (no store wrap) is mapped."""

    def failing(_: Path) -> None:
        raise pl.exceptions.ComputeError("bad column block")

    with pytest.raises(ArtifactLoadError) as excinfo:
        guarded_load(failing, tmp_path / "x.parquet")
    assert isinstance(excinfo.value.__cause__, pl.exceptions.ComputeError)


def test_g14_pyarrow_error_maps_to_artifact_load_error(tmp_path: Path) -> None:
    """A RAW pyarrow error escaping a loader is mapped."""

    def failing(_: Path) -> None:
        raise pyarrow.lib.ArrowInvalid("Parquet magic bytes not found")

    with pytest.raises(ArtifactLoadError) as excinfo:
        guarded_load(failing, tmp_path / "x.parquet")
    assert isinstance(excinfo.value.__cause__, pyarrow.lib.ArrowInvalid)


def test_g14_success_passes_the_value_through_unchanged(tmp_path: Path) -> None:
    sentinel = object()

    def loader(path: Path) -> object:
        assert path == tmp_path / "ok.parquet"
        return sentinel

    assert guarded_load(loader, tmp_path / "ok.parquet") is sentinel


def test_g14_store_column_names_exact() -> None:
    """The legacy-KeyError discriminator vocabulary — exact, mutation-hardened."""
    assert (
        frozenset(
            {
                "run_id",
                "instrument",
                "tf",
                "direction",
                "mode",
                "condition_names",
                "mask_bits_lo",
                "mask_bits_hi",
                "net_pnl_paisa",
                "gross_profit_paisa",
                "gross_loss_paisa",
                "max_drawdown_paisa",
                "expectancy_paisa",
                "total_trades",
                "winning_trades",
                "losing_trades",
                "sharpe_ratio",
                "sortino_ratio",
                "calmar_ratio",
                "profit_factor",
                "win_rate",
                "max_drawdown_pct",
                "deflated_sharpe",
                "avg_entry_price",
                "avg_exit_price",
            }
        )
        == STORE_COLUMN_NAMES
    )


def test_g14_optional_error_set_with_both_libraries() -> None:
    """With polars + pyarrow importable, both base classes join the caught set."""
    errors = sweep_artifact_guard._optional_parquet_errors()  # pyright: ignore[reportPrivateUsage]
    assert errors == (pl.exceptions.PolarsError, pyarrow.lib.ArrowException)


def test_g14_optional_error_set_wheel_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a wheel-free install each arm drops out — the set degrades to ()."""
    monkeypatch.setitem(sys.modules, "polars", None)
    monkeypatch.setitem(sys.modules, "pyarrow.lib", None)
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    assert sweep_artifact_guard._optional_parquet_errors() == ()  # pyright: ignore[reportPrivateUsage]


def test_g14_load_failed_warn_logged_once(tmp_path: Path) -> None:
    """Exactly ONE sweep_artifact_guard.load_failed WARN per mapped failure."""
    artifact = tmp_path / "legacy.parquet"

    def raw_legacy_loader(_: Path) -> None:
        raise KeyError("mask_bits_lo")

    with capture_logs() as logs, pytest.raises(ArtifactLoadError):
        guarded_load(raw_legacy_loader, artifact)
    warns = [e for e in logs if e["event"] == "sweep_artifact_guard.load_failed"]
    assert len(warns) == 1
    assert warns[0]["path"] == str(artifact)
