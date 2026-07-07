"""Golden byte-freeze of the three engine run-identity digests (guard-parity pins).

Byte-freezes — landed BEFORE the DEC-RUN-IDENTITY-GUARD-PARITY-001 reject-only
guards so the guards must pass these pins UNCHANGED — the exact BLAKE3 digests
of the three engine run-identity joins:

* :func:`brutex.engine.stage3_refine.refinement_run_id` (ADR-0007 condition 4),
* ``brutex.engine.sweep_driver._walk_forward_run_hash``
  (DEC-WALK-FORWARD-OOS-001),
* ``brutex.engine.stage1a._walk_forward_schedule_hash``.

Every corpus case ALSO hand-assembles the documented payload BYTES (field
order, the ``stage3-refine-v1`` / ``walk_forward`` labels, the
``RefineBudget.canonical()`` encoding, the canonical-JSON key order +
``(",", ":")`` separators, the ``\\x1f`` join) and asserts the SAME frozen
digest through THREE independent routes:

1. the production function (the wire contract);
2. ``fno_math.blake3_hash`` over the hand-assembled payload — pins the payload
   BYTES, not just the digest;
3. the independent pip ``blake3`` package over the same payload — proves
   ``fno_math.blake3_hash`` computes real BLAKE3, byte-for-byte.

The library-version identity fields are LITERAL strings (``"4.0.0"`` /
``"2.1.0"``), never the live ``cma.__version__`` / ``numpy.__version__``, so
these digests survive dependency bumps. Any digest movement here is a
run-identity break (Golden Rule #4).

Whitebox by design: imports the two private engine hash helpers, so per the
DEC-P0-017 convention the file silences ``reportPrivateUsage`` at the top.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Callable

import blake3
import fno_math
from brutex.engine.stage1a import WalkForwardFold, _walk_forward_schedule_hash
from brutex.engine.stage3_refine import RefineBudget, refinement_run_id
from brutex.engine.sweep_driver import _walk_forward_run_hash
from brutex.strategies.types import Side
from brutex.vocabulary import ProximityParams

#: The documented run-identity field-join delimiter (the combo_hash convention).
_SEP = "\x1f"

#: A valid 64-char BLAKE3-shaped mask run identity (deterministic fixture).
_MASK_RUN_ID = "ab" * 32

#: A valid 64-char BLAKE3-shaped fold-schedule hash (deterministic fixture).
_SCHEDULE_HASH = "cd" * 32


def _assert_pinned(name: str, produced: str, payload: str, frozen: str) -> None:
    """Assert production digest + kernel oracle + pip-blake3 oracle all equal *frozen*."""
    assert produced == frozen, f"{name}: production digest moved"
    assert fno_math.blake3_hash(payload.encode("utf-8")).hex() == frozen, (
        f"{name}: hand-assembled payload bytes no longer hash to the frozen digest"
    )
    assert blake3.blake3(payload.encode("utf-8")).hexdigest() == frozen, (
        f"{name}: fno_math.blake3_hash diverges from the independent pip blake3"
    )


# ────────────────────────────────────────────────────────────────────────
# refinement_run_id (stage3_refine) — default / varied budget / seed / versions
# ────────────────────────────────────────────────────────────────────────

_REFINE_FROZEN: dict[str, str] = {
    "default": "7b12c0c4e8a094ce61bf8cf1f321e1da9f0dd67f32b849a272fe409db1e6646d",
    "varied_budget": "d45b6a118543b4304d2104847098ae968c798419f8f022413b507de297fae764",
    "varied_seed": "14d6f6b8c820e1b47368a1fe14271a25b22b0cd7e395980739fd4ee9ab0e5c1b",
    "varied_versions": "0d2c04cb450ac199c12e54b0a2ca7fafb2e2712df4615b8fde4986d17518c7c8",
}

#: Hand-assembled payload bytes per case — the documented ``blake3(mask_run_id
#: ‖ optimizer-config ‖ seed ‖ budget ‖ library versions)`` encoding under the
#: ``stage3-refine-v1`` label, ``budget.canonical()`` written out literally.
_REFINE_PAYLOADS: dict[str, str] = {
    "default": _SEP.join(
        (
            "stage3-refine-v1",
            _MASK_RUN_ID,
            "fevals=200;restarts=3;sigma0=0.3",
            "seed=7",
            "cma=4.0.0",
            "numpy=2.1.0",
        )
    ),
    "varied_budget": _SEP.join(
        (
            "stage3-refine-v1",
            _MASK_RUN_ID,
            "fevals=50;restarts=2;sigma0=0.5",
            "seed=7",
            "cma=4.0.0",
            "numpy=2.1.0",
        )
    ),
    "varied_seed": _SEP.join(
        (
            "stage3-refine-v1",
            _MASK_RUN_ID,
            "fevals=200;restarts=3;sigma0=0.3",
            "seed=123456789",
            "cma=4.0.0",
            "numpy=2.1.0",
        )
    ),
    "varied_versions": _SEP.join(
        (
            "stage3-refine-v1",
            _MASK_RUN_ID,
            "fevals=200;restarts=3;sigma0=0.3",
            "seed=7",
            "cma=3.3.0",
            "numpy=1.26.4",
        )
    ),
}

_REFINE_CALLS: dict[str, Callable[[], str]] = {
    "default": lambda: refinement_run_id(
        mask_run_id=_MASK_RUN_ID,
        budget=RefineBudget(),
        seed=7,
        pycma_version="4.0.0",
        numpy_version="2.1.0",
    ),
    "varied_budget": lambda: refinement_run_id(
        mask_run_id=_MASK_RUN_ID,
        budget=RefineBudget(max_fevals=50, max_restarts=2, sigma0=0.5),
        seed=7,
        pycma_version="4.0.0",
        numpy_version="2.1.0",
    ),
    "varied_seed": lambda: refinement_run_id(
        mask_run_id=_MASK_RUN_ID,
        budget=RefineBudget(),
        seed=123456789,
        pycma_version="4.0.0",
        numpy_version="2.1.0",
    ),
    "varied_versions": lambda: refinement_run_id(
        mask_run_id=_MASK_RUN_ID,
        budget=RefineBudget(),
        seed=7,
        pycma_version="3.3.0",
        numpy_version="1.26.4",
    ),
}


def test_refinement_run_id_digests_frozen() -> None:
    """Each refinement-run identity reproduces its pinned digest via all 3 routes."""
    for name, frozen in _REFINE_FROZEN.items():
        _assert_pinned(name, _REFINE_CALLS[name](), _REFINE_PAYLOADS[name], frozen)


# ────────────────────────────────────────────────────────────────────────
# _walk_forward_run_hash (sweep_driver) — proximity aliasing / SL-TP / direction
# ────────────────────────────────────────────────────────────────────────

_WF_FROZEN: dict[str, str] = {
    "none_proximity": "8247d6783f28b43fe4a6cd66eb4400d717b51d736fa35e40c20d0efe4c38b4d1",
    "explicit_proximity": "024a796e8de00f6780879effb94f3fd849fb51e3e0da3cf5ef7d734245b08561",
    "sl_tp_set": "fe81367f14ba2c59668425e79ab8d6e7ff1708cf525a44d8bb6841dcd0d54156",
    "sell": "fc45f2ef7edc15c059b931b26ebdd708124ec362c34d2363d3966d8ac372f77d",
}

#: Hand-assembled ``walk_forward ‖ schedule_hash ‖ canonical-JSON`` payloads —
#: the canonical JSON written out literally (sorted keys, ``,``/``:``
#: separators, ``null`` for an unset SL/TP).
_WF_PAYLOADS: dict[str, str] = {
    "none_proximity": _SEP.join(
        (
            "walk_forward",
            _SCHEDULE_HASH,
            '{"direction":"BUY","fib_proximity_bps":10,"mask":11,'
            '"pdh_pdl_proximity_bps":10,"pivot_proximity_bps":10,"prior_n":3,'
            '"start_equity_paisa":10000000,"stop_loss_paisa":null,'
            '"take_profit_paisa":null,"tf_minutes":5}',
        )
    ),
    "explicit_proximity": _SEP.join(
        (
            "walk_forward",
            _SCHEDULE_HASH,
            '{"direction":"BUY","fib_proximity_bps":50,"mask":11,'
            '"pdh_pdl_proximity_bps":5,"pivot_proximity_bps":25,"prior_n":3,'
            '"start_equity_paisa":10000000,"stop_loss_paisa":null,'
            '"take_profit_paisa":null,"tf_minutes":5}',
        )
    ),
    "sl_tp_set": _SEP.join(
        (
            "walk_forward",
            _SCHEDULE_HASH,
            '{"direction":"BUY","fib_proximity_bps":10,"mask":11,'
            '"pdh_pdl_proximity_bps":10,"pivot_proximity_bps":10,"prior_n":3,'
            '"start_equity_paisa":10000000,"stop_loss_paisa":1500,'
            '"take_profit_paisa":4500,"tf_minutes":5}',
        )
    ),
    "sell": _SEP.join(
        (
            "walk_forward",
            _SCHEDULE_HASH,
            '{"direction":"SELL","fib_proximity_bps":10,"mask":11,'
            '"pdh_pdl_proximity_bps":10,"pivot_proximity_bps":10,"prior_n":3,'
            '"start_equity_paisa":10000000,"stop_loss_paisa":null,'
            '"take_profit_paisa":null,"tf_minutes":5}',
        )
    ),
}


def _wf_hash(
    *,
    direction: Side = Side.BUY,
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    proximity: ProximityParams | None = None,
) -> str:
    """Call the production walk-forward run-hash with the corpus base fields."""
    return _walk_forward_run_hash(
        _SCHEDULE_HASH,
        required_mask=11,
        direction=direction,
        tf_minutes=5,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        start_equity_paisa=10_000_000,
        proximity=proximity,
        prior_n=3,
    )


_WF_CALLS: dict[str, Callable[[], str]] = {
    "none_proximity": _wf_hash,
    "explicit_proximity": lambda: _wf_hash(
        proximity=ProximityParams(
            pivot_proximity_bps=25, pdh_pdl_proximity_bps=5, fib_proximity_bps=50
        )
    ),
    "sl_tp_set": lambda: _wf_hash(stop_loss_paisa=1500, take_profit_paisa=4500),
    "sell": lambda: _wf_hash(direction=Side.SELL),
}


def test_walk_forward_run_hash_digests_frozen() -> None:
    """Each walk-forward run-hash reproduces its pinned digest via all 3 routes."""
    for name, frozen in _WF_FROZEN.items():
        _assert_pinned(name, _WF_CALLS[name](), _WF_PAYLOADS[name], frozen)


def test_walk_forward_none_proximity_aliases_default_proximity() -> None:
    """``proximity=None`` deliberately aliases ``ProximityParams()`` (same digest).

    Pins the ``prox = proximity if proximity is not None else ProximityParams()``
    normalisation: an unset proximity and the explicit defaults are the SAME
    run identity — freezing this stops a future "fix" from silently re-keying
    every persisted walk-forward result.
    """
    assert _wf_hash(proximity=ProximityParams()) == _WF_FROZEN["none_proximity"]
    assert _wf_hash(proximity=None) == _WF_FROZEN["none_proximity"]


# ────────────────────────────────────────────────────────────────────────
# _walk_forward_schedule_hash (stage1a) — hand-built fold layouts
# ────────────────────────────────────────────────────────────────────────

_SCHED_FROZEN: dict[str, str] = {
    "two_folds": "b7992a6c15697d7c172717b6d7810db5dd05ece3618275c777d306b2c70f2366",
    "one_fold": "ca5b1b16cc110b856acc8da097b44aeb1cb0bbbf04cd338cb2f29df7cbf362e5",
}

#: Hand-assembled canonical-JSON payloads (sorted keys; folds serialised as
#: ``[train_start_bar, train_end_bar, test_start_bar, test_end_bar]`` under
#: version tag ``"v":1`` — the day bounds are deliberately NOT in the hash).
_SCHED_PAYLOADS: dict[str, str] = {
    "two_folds": (
        '{"folds":[[0,10,10,12],[2,12,12,14]],"n_bars":14,"n_trading_days":7,'
        '"step_days":1,"test_days":1,"train_days":5,"v":1}'
    ),
    "one_fold": (
        '{"folds":[[0,6,6,8]],"n_bars":8,"n_trading_days":4,'
        '"step_days":1,"test_days":1,"train_days":3,"v":1}'
    ),
}


def _fold(
    fold_index: int,
    train_days: tuple[int, int],
    test_days: tuple[int, int],
    train_bars: tuple[int, int],
    test_bars: tuple[int, int],
) -> WalkForwardFold:
    """Build one hand-specified walk-forward fold (both coordinate systems)."""
    return WalkForwardFold(
        fold_index=fold_index,
        train_start_day=train_days[0],
        train_end_day=train_days[1],
        test_start_day=test_days[0],
        test_end_day=test_days[1],
        train_start_bar=train_bars[0],
        train_end_bar=train_bars[1],
        test_start_bar=test_bars[0],
        test_end_bar=test_bars[1],
    )


_SCHED_CALLS: dict[str, Callable[[], str]] = {
    "two_folds": lambda: _walk_forward_schedule_hash(
        (
            _fold(0, (0, 5), (5, 6), (0, 10), (10, 12)),
            _fold(1, (1, 6), (6, 7), (2, 12), (12, 14)),
        ),
        5,
        1,
        1,
        7,
        14,
    ),
    "one_fold": lambda: _walk_forward_schedule_hash(
        (_fold(0, (0, 3), (3, 4), (0, 6), (6, 8)),),
        3,
        1,
        1,
        4,
        8,
    ),
}


def test_walk_forward_schedule_hash_digests_frozen() -> None:
    """Each schedule hash reproduces its pinned digest via all 3 routes."""
    for name, frozen in _SCHED_FROZEN.items():
        _assert_pinned(name, _SCHED_CALLS[name](), _SCHED_PAYLOADS[name], frozen)


# ────────────────────────────────────────────────────────────────────────
# Cross-corpus shape + sensitivity
# ────────────────────────────────────────────────────────────────────────


def test_all_frozen_digests_distinct() -> None:
    """The 10 frozen digests are pairwise distinct (per-field sensitivity)."""
    digests = [*_REFINE_FROZEN.values(), *_WF_FROZEN.values(), *_SCHED_FROZEN.values()]
    assert len(set(digests)) == len(digests) == 10


def test_every_frozen_digest_is_64_lowercase_hex() -> None:
    """BLAKE3-256 output shape: 64 lowercase hex chars each."""
    for frozen in (*_REFINE_FROZEN.values(), *_WF_FROZEN.values(), *_SCHED_FROZEN.values()):
        assert len(frozen) == 64
        assert frozen == frozen.lower()
        assert all(c in "0123456789abcdef" for c in frozen)
