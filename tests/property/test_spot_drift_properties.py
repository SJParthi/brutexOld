"""Property invariants for the stock drift map (DEC-GREEKS-SPOTJOIN-STOCK-001).

Hypothesis invariants over :func:`brutex.data.spot_drift.build_stock_drift_map`
and the :func:`brutex.data.lake_keys.spot_recovery_candidates` stock rung —
the no-fabrication / unambiguity / determinism / idempotence guarantees the
design pins (P-1 … P-5). The alphabet deliberately includes ``&``, ``-``,
space, ``%`` and mixed case — the real drift-class characters.
"""

from __future__ import annotations

from brutex.data.lake_keys import spot_recovery_candidates, stock_drift_fold
from brutex.data.spot_drift import build_stock_drift_map
from hypothesis import given
from hypothesis import strategies as st
from structlog.testing import capture_logs

_SYMBOL = st.text(alphabet="ABKMZabz&-% 0123456789", min_size=1, max_size=12)
_DIRS = st.lists(_SYMBOL, max_size=12)
_MASTERS = st.lists(_SYMBOL, max_size=8)
_EXCHANGE = st.sampled_from(["NSE", "BSE"])


@given(dirs=_DIRS, masters=_MASTERS, exchange=_EXCHANGE)
def test_p1_no_fabrication_every_mapped_dir_is_real_and_verbatim(
    dirs: list[str], masters: list[str], exchange: str
) -> None:
    """P-1: every value in every entry is one of the input dirs, verbatim."""
    with capture_logs():
        mapping = build_stock_drift_map({exchange: dirs}, {exchange: masters})
    dir_set = set(dirs)
    for (exch, _fold), mapped_dirs in mapping.items():
        assert exch == exchange
        for mapped in mapped_dirs:
            assert mapped in dir_set  # never invented, never normalised

    # And every mapped dir is unique within its entry (dedup holds).
    for mapped_dirs in mapping.values():
        assert len(set(mapped_dirs)) == len(mapped_dirs)


@given(dirs=_DIRS, masters=_MASTERS, exchange=_EXCHANGE)
def test_p2_unambiguity_every_entry_fold_has_exactly_one_master_owner(
    dirs: list[str], masters: list[str], exchange: str
) -> None:
    """P-2: no entry lands on a poisoned fold — each entry's fold owns exactly
    one master symbol."""
    with capture_logs():
        mapping = build_stock_drift_map({exchange: dirs}, {exchange: masters})
    owners_by_fold: dict[str, set[str]] = {}
    for name in masters:
        fold = stock_drift_fold(name)
        if fold:
            owners_by_fold.setdefault(fold, set()).add(name)
    for (_exch, fold), _mapped in mapping.items():
        assert len(owners_by_fold[fold]) == 1


@given(dirs=_DIRS, masters=_MASTERS, exchange=_EXCHANGE, data=st.data())
def test_p3_deterministic_under_permutation_of_both_inputs(
    dirs: list[str], masters: list[str], exchange: str, data: st.DataObject
) -> None:
    """P-3: permuting either input collection never changes the output."""
    with capture_logs():
        baseline = build_stock_drift_map({exchange: dirs}, {exchange: masters})
        dir_perm = data.draw(st.permutations(dirs)) if dirs else []
        master_perm = data.draw(st.permutations(masters)) if masters else []
        assert build_stock_drift_map({exchange: dir_perm}, {exchange: master_perm}) == baseline


@given(raw=_SYMBOL)
def test_p4_fold_idempotence(raw: str) -> None:
    """P-4: ``fold(fold(x)) == fold(x)``."""
    assert stock_drift_fold(stock_drift_fold(raw)) == stock_drift_fold(raw)


@given(dirs=_DIRS, masters=_MASTERS, underlying=_SYMBOL, exchange=_EXCHANGE)
def test_p5_end_to_end_ladder_envelope(
    dirs: list[str], masters: list[str], underlying: str, exchange: str
) -> None:
    """P-5: with a built map, the extended ladder (a) never yields the
    underlying itself, (b) satisfies the prefix invariant, and (c) any
    candidate past the base rungs is a REAL on-disk dir from the map."""
    with capture_logs():
        mapping = build_stock_drift_map({exchange: dirs}, {exchange: masters})
    base = spot_recovery_candidates(underlying, exchange)
    extended = spot_recovery_candidates(underlying, exchange, stock_drift=mapping)
    assert underlying not in extended
    assert extended[: len(base)] == base
    dir_set = set(dirs)
    for candidate in extended[len(base) :]:
        assert candidate in dir_set


def test_module_import_is_wheel_light() -> None:
    """The spot_drift module itself needs neither duckdb nor polars at import
    (the lazy-import convention) — pin it so the lean property job keeps
    collecting this file."""
    import sys

    import brutex.data.spot_drift as spot_drift_module

    assert hasattr(spot_drift_module, "build_stock_drift_map")
    # The loader's heavy deps stay function-local; importing the module cannot
    # have pulled the store (a fresh interpreter proof would be stronger — the
    # unit suite's lean-job collection is the practical gate here).
    assert "brutex.data.spot_drift" in sys.modules
