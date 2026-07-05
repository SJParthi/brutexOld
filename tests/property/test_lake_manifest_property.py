"""Property invariants for the lake dataset manifest (DEC-LAKE-MANIFEST-O1-001).

Hypothesis-driven, over arbitrary coordinate row sets (symbols deliberately
include the #911 drift-key alphabet — ``&``, ``-``, ``.``, space):

* **Round-trip canonicalisation** — ``load(rebuild(rows))`` equals the
  DEDUPLICATED row set sorted by the four coordinate keys, for ANY input
  ordering / duplication.
* **Rebuild idempotence** — a second rebuild of the same rows is a no-op
  (byte-identical data file), the Rule-7 rerun-safety oracle.
* **Union monotonicity** — a rebuild never DROPS a row already on disk
  (keep-first union: writer-recorded truth survives a narrower walk).
* **Filter equivalence** — the manifest projection filters
  (``exclude_segments`` / ``intervals``) match a plain Python filter of the
  same canonical rows (the walk-equivalence kernel the byte-identity
  integration test pins against real directories).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from brutex.data import lake_manifest as lm
from brutex.data.partition import VALID_EXCHANGES, VALID_INTERVALS, VALID_SEGMENTS
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# NOTE (lean-CI honesty): `brutex.dashboard.db_view` imports `duckdb` + the
# heavy dashboard graph at module level, and the property job installs only
# the lean `.[dev]` extra (its sibling suites skip on the missing heavy deps).
# The manifest-only properties below must still RUN there (`lake_manifest` is
# a stdlib+leaf module by design — DEC-LAKE-MANIFEST-O1-001), so the ONE
# db_view-dependent property is guarded by a module-level TRY-import + a
# `skipif` marker (a decorator skip — never a mid-`@given` skip) instead of
# skipping the whole module.
try:
    from brutex.dashboard import db_view as _db_view
except ModuleNotFoundError:  # pragma: no cover — the lean .[dev]-only CI job
    _db_view = None  # type: ignore[assignment]

_HAS_DB_VIEW = _db_view is not None

_SYMBOLS = st.text(
    alphabet="ABCXYZ&-. 123",
    min_size=1,
    max_size=12,
).filter(lambda s: s.strip() == s and s != "")

_ROWS = st.lists(
    st.fixed_dictionaries(
        {
            "exchange": st.sampled_from(["NSE", "BSE"]),
            "segment": st.sampled_from(["INDEX", "CASH", "FNO"]),
            "symbol": _SYMBOLS,
            "interval": st.sampled_from(["1minute", "5minute", "1day"]),
        }
    ),
    max_size=12,
)

_PROPERTY_SETTINGS = settings(
    max_examples=25,  # each example does real (tmp-dir) file IO
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def _canonical(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Dedup keep-first + sort by the four keys — the manifest's contract."""
    seen: dict[tuple[str, str, str, str], dict[str, str]] = {}
    for row in rows:
        key = (row["exchange"], row["segment"], row["symbol"], row["interval"])
        seen.setdefault(key, dict(row))
    return [seen[key] for key in sorted(seen)]


@_PROPERTY_SETTINGS
@given(rows=_ROWS)
def test_rebuild_load_roundtrip_is_canonical(rows: list[dict[str, str]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        lake = Path(tmp)
        token = lm.ledger_stat_token(lake)
        assert token is not None
        assert lm.rebuild_manifest(lake, rows, ledger_token=token) is True
        assert lm.load_manifest_datasets(lake) == _canonical(rows)


@_PROPERTY_SETTINGS
@given(rows=_ROWS)
def test_rebuild_is_idempotent_bit_for_bit(rows: list[dict[str, str]]) -> None:
    """Same inputs → same on-disk ROW bytes (Rule 7; ``first_seen`` re-stamps
    with the union's rebuild time, so the pin is on the coordinate content)."""
    with tempfile.TemporaryDirectory() as tmp:
        lake = Path(tmp)
        token = lm.ledger_stat_token(lake)
        assert token is not None
        lm.rebuild_manifest(lake, rows, ledger_token=token)
        first = lm.load_manifest_datasets(lake)
        lm.rebuild_manifest(lake, rows, ledger_token=token)
        assert lm.load_manifest_datasets(lake) == first


@_PROPERTY_SETTINGS
@given(rows=_ROWS, extra=_ROWS)
def test_rebuild_union_never_drops_on_disk_rows(
    rows: list[dict[str, str]], extra: list[dict[str, str]]
) -> None:
    """A rebuild from a NARROWER walk keeps every row already recorded on disk."""
    with tempfile.TemporaryDirectory() as tmp:
        lake = Path(tmp)
        token = lm.ledger_stat_token(lake)
        assert token is not None
        lm.rebuild_manifest(lake, rows + extra, ledger_token=token)
        lm.rebuild_manifest(lake, rows, ledger_token=token)  # narrower walk
        loaded = lm.load_manifest_datasets(lake)
        assert loaded == _canonical(rows + extra)


@pytest.mark.skipif(not _HAS_DB_VIEW, reason="db_view needs duckdb (the heavy extra)")
@_PROPERTY_SETTINGS
@given(
    rows=_ROWS,
    exclude=st.frozensets(st.sampled_from(["INDEX", "CASH", "FNO"]), max_size=2),
    interval_filter=st.none() | st.lists(st.sampled_from(["1minute", "1day"]), max_size=2),
)
def test_manifest_projection_matches_plain_filter(
    rows: list[dict[str, str]],
    exclude: frozenset[str],
    interval_filter: list[str] | None,
) -> None:
    """``_datasets_from_manifest`` == a plain Python filter over canonical rows.

    The projection's real input domain is ``load_manifest_datasets``'s output —
    already DEDUPED keep-first + validated — so the property feeds the same
    canonical form (a raw duplicated list is collapsed by the loader, never
    handed to the projection).
    """
    assert _db_view is not None  # skipif-guarded; narrows for the type checkers
    projected = _db_view._datasets_from_manifest(
        _canonical(rows), exclude_segments=exclude, intervals=interval_filter
    )
    expected = [
        {
            "kind": "spot",
            "exchange": row["exchange"],
            "segment": row["segment"],
            "symbol": row["symbol"],
            "interval": row["interval"],
        }
        for row in _canonical(rows)
        if row["exchange"] in VALID_EXCHANGES
        and row["segment"] in VALID_SEGMENTS
        and row["segment"] not in exclude
        and row["interval"] in VALID_INTERVALS
        and (interval_filter is None or row["interval"] in interval_filter)
    ]
    assert projected == expected
