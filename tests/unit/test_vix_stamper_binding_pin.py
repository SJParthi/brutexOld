"""AST binding pin for the India-VIX ``reference_stamper`` seam (DEC-VIX-STAMP-WIRE-001).

The coordinator ruling: the stamper seam is threaded through EVERY production
``simulate_trades`` path, and a DISCARDED inner trade stream may stay unstamped
ONLY via an EXPLICIT literal ``reference_stamper=None`` at an enumerated,
comment-justified site — never a silent omission a refactor could hide. This
pin walks the AST of every production module under ``brutex/`` and enforces:

1. a literal ``reference_stamper=None`` (or ``stamper_for=None``) keyword on a
   seam call is allowed ONLY at the exactly-two sanctioned discarded-stream
   sites — the sweep's K-of-N inner mask stream
   (``sweep_run.py`` → ``_build_trade_sim``) and the cascade's Monte-Carlo
   permutation filter (``cascade_wire.py`` → ``simulate_trades``) — each with
   an EXACT count of one, so BOTH removing a sanctioned literal AND adding a
   new one fail loudly;
2. no seam call smuggles the binding through a ``**kwargs`` splat (which could
   hide a ``None`` from this pin and from review).

The positive binds (every observable path actually receives the stamper) are
proven behaviourally by ``tests/unit/test_engine_vix_stamper_wiring.py``; this
pin owns the never-silently-unstamped invariant.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final, NamedTuple

import brutex

#: Every callable carrying the ``reference_stamper`` seam (or the ``stamper_for``
#: provider seam) after Stage B. A call to ANY of these names is a seam call.
_SEAM_CALLEES: Final[frozenset[str]] = frozenset(
    {
        # trade-sim core
        "simulate_trades",
        "TradeSimWalker",
        "ResidentTradeSim",
        # sweep_driver
        "evaluate_combo",
        "metrics_for_mask",
        "_simulate_window",
        "sweep_masks",
        "rank_sweep",
        "rank_sweep_over_bits",
        "_score_fold",
        "evaluate_combo_walk_forward",
        "walk_forward_sweep",
        # stage-3 validators
        "validate_out_of_sample",
        "combo_returns",
        "validate_candidate",
        # sweep_report
        "oos_section",
        "filter_oos_held",
        "sweep_report",
        # sweep_run
        "run_persisted_sweep",
        "_build_trade_sim",
        "_reattribute_chunk_failure",
        "ranked_combo_to_result_row",
        # grid drivers
        "run_one_tuple",
        "run_sweep_grid",
        "run_supervised_grid",
        "run_parallel_grid",
        "ParallelGridDriver",
        "_WorkerContext",
        # stage-3 wiring
        "run_stage3_pass",
        "_make_evaluator",
        # cascade wiring
        "build_candidate_input",
        "candidates_for_spec",
        "run_cascade_over_specs",
        "run_and_write_from_specs",
        "run_and_write_from_manifest",
        # CLI replay
        "_replay_spot_signals_for_row",
        "_options_sim_for_row",
    }
)

#: The stamper-carrying keyword names this pin polices.
_SEAM_KWARGS: Final[frozenset[str]] = frozenset({"reference_stamper", "stamper_for"})

#: The EXACTLY-TWO sanctioned literal-None discarded-stream sites, keyed by
#: ``(module basename, callee name)`` → the exact number of literal-``None``
#: bindings that module may carry on that callee. Anything else is a violation.
_ALLOWED_LITERAL_NONE: Final[dict[tuple[str, str], int]] = {
    # The K-of-N inner mask stream — trades discarded after the metrics tail.
    ("sweep_run.py", "_build_trade_sim"): 1,
    # The Monte-Carlo permutation filter — the discarded null distribution.
    ("cascade_wire.py", "simulate_trades"): 1,
}


class _SeamCall(NamedTuple):
    """One seam call site: where it is + how it binds the stamper kwarg."""

    module: str  # repo-relative path
    basename: str
    callee: str
    lineno: int
    literal_none: bool  # reference_stamper=None / stamper_for=None literal
    has_splat: bool  # **kwargs at the call


def _callee_name(node: ast.Call) -> str | None:
    """The called name — ``Name.id`` or the final ``Attribute.attr``."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _iter_seam_calls() -> list[_SeamCall]:
    """Every seam call in every production module under ``brutex/``."""
    package_root = Path(brutex.__file__).resolve().parent
    calls: list[_SeamCall] = []
    for path in sorted(package_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = str(path.relative_to(package_root.parent))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = _callee_name(node)
            if callee not in _SEAM_CALLEES:
                continue
            literal_none = any(
                kw.arg in _SEAM_KWARGS
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is None
                for kw in node.keywords
            )
            has_splat = any(kw.arg is None for kw in node.keywords)
            calls.append(
                _SeamCall(
                    module=rel,
                    basename=path.name,
                    callee=callee,
                    lineno=node.lineno,
                    literal_none=literal_none,
                    has_splat=has_splat,
                )
            )
    return calls


class TestVixStamperBindingPin:
    def test_seam_calls_are_actually_scanned(self) -> None:
        """The scanner sees the production seam surface (a moved/renamed seam
        must update this pin consciously, never silently escape it)."""
        calls = _iter_seam_calls()
        assert len(calls) >= 30, f"expected a wide seam surface, saw {len(calls)}"

    def test_literal_none_only_at_the_two_sanctioned_sites_with_exact_counts(self) -> None:
        literal_sites: dict[tuple[str, str], int] = {}
        offenders: list[str] = []
        for call in _iter_seam_calls():
            if not call.literal_none:
                continue
            key = (call.basename, call.callee)
            literal_sites[key] = literal_sites.get(key, 0) + 1
            if key not in _ALLOWED_LITERAL_NONE:
                offenders.append(f"{call.module}:{call.lineno} {call.callee}(…=None)")
        assert not offenders, (
            "literal reference_stamper=None outside the two sanctioned "
            f"discarded-stream sites: {offenders} — bind the run's stamper (or add "
            "an honest-envelope comment + extend the sanctioned map via a conscious "
            "review, never silently)"
        )
        # EXACT counts: both sanctioned literals must EXIST (removing one —
        # e.g. deleting the explicit None while leaving the default — silently
        # converts a stated decision into an omission) and must not multiply.
        assert literal_sites == _ALLOWED_LITERAL_NONE

    def test_no_seam_call_splats_kwargs(self) -> None:
        splatted = [
            f"{call.module}:{call.lineno} {call.callee}(**…)"
            for call in _iter_seam_calls()
            if call.has_splat
        ]
        assert not splatted, (
            f"seam calls must bind reference_stamper explicitly, never via a "
            f"**kwargs splat that could hide a None: {splatted}"
        )
