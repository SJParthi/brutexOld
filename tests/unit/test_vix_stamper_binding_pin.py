"""AST binding pin for the India-VIX ``reference_stamper`` seam (DEC-VIX-STAMP-WIRE-001).

The coordinator ruling: the stamper seam is threaded through EVERY production
``simulate_trades`` path, and a DISCARDED trade stream may stay unstamped ONLY
via an EXPLICIT literal ``reference_stamper=None`` at an enumerated,
comment-justified site. This pin walks the AST of every production module
under ``brutex/`` and enforces THREE exact-count laws (each two-sided — both
removing an enumerated entry AND adding a new one fail loudly):

1. **literal-None** — a literal ``reference_stamper=None`` (or
   ``stamper_for=None``) on a seam call is allowed ONLY at the enumerated
   sanctioned discarded-stream sites in :data:`_ALLOWED_LITERAL_NONE`;
2. **omission** — a seam call that carries NO stamper kwarg at all silently
   re-dormants the feature behind the default ``None`` (the round-2 panel
   found THREE such shipped omissions: ``cmd_sweep``'s ``sweep_report`` and
   the recount's ``run_sweep_grid``/``run_stage3_pass``); every omission must
   now be enumerated in :data:`_ALLOWED_OMITTED` (currently EMPTY — every
   production seam call states its binding explicitly);
3. **positive presence** — the pragma-no-cover production ENTRY POINTS
   (``brutex/cli.py`` + ``brutex/dashboard/app.py``) must carry a NON-None
   binding at exactly the calls enumerated in
   :data:`_REQUIRED_BOUND_ENTRYPOINTS`, so deleting a
   ``reference_stamper=reference_stamper`` kwarg at a launch site (the
   #935/#1014 dormant-binding trap) fails this pin even though the shims have
   no unit coverage.

Plus: no seam call may smuggle the binding through a ``**kwargs`` splat.

HONEST ENVELOPE (Golden Rule #10): the scanner matches the CALLED NAME — a
bare ``Name`` or the final ``Attribute`` — against :data:`_SEAM_CALLEES`; an
aliased import (``import x as y``) or ``getattr`` indirection at a seam call
would escape it. No such indirection exists in ``brutex/`` today, and the
behavioural wiring suite (``tests/unit/test_engine_vix_stamper_wiring.py``)
plus the real-lake integration suite
(``tests/integration/test_cli_sweep_all_vix_stamp.py``) prove the binds
END-TO-END where they execute — this pin owns the STATIC
never-silently-unstamped invariant, not a claim of alias-proof coverage.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final, NamedTuple

import brutex

#: Every callable carrying the ``reference_stamper`` seam (or the ``stamper_for``
#: provider seam) after Stage B. A call to ANY of these names is a seam call.
#: (The CLI options-sim helpers ``_replay_spot_signals_for_row`` /
#: ``_options_sim_for_row`` carried the seam in Stage B and were REMOVED from
#: it by the round-2 review — their stamps were provably discarded at the
#: SpotSignal conversion, so the seam now ends at the sanctioned literal-None
#: ``simulate_trades`` call inside the replay helper.)
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
    }
)

#: The stamper-carrying keyword names this pin polices.
_SEAM_KWARGS: Final[frozenset[str]] = frozenset({"reference_stamper", "stamper_for"})

#: The sanctioned literal-None discarded-stream sites, keyed by
#: ``(repo-relative module path, callee name)`` → the exact number of
#: literal-``None`` bindings that module may carry on that callee. Keyed by
#: the FULL path (round-2 NIT): a basename key would let a same-named file
#: elsewhere silently relocate a sanctioned exemption.
_ALLOWED_LITERAL_NONE: Final[dict[tuple[str, str], int]] = {
    # The K-of-N inner mask stream — trades discarded after the metrics tail.
    ("brutex/engine/sweep_run.py", "_build_trade_sim"): 1,
    # The Monte-Carlo permutation filter — the discarded null distribution.
    ("brutex/engine/cascade_wire.py", "simulate_trades"): 1,
    # The options-sim replay — stamps would be discarded at the SpotSignal
    # conversion (signals_from_spot_trades reads only entry/exit/direction).
    ("brutex/cli.py", "simulate_trades"): 1,
    # `brutex sweep`'s text leaderboard — report bytes are pinned identical
    # stamped-vs-unstamped and nothing is persisted.
    ("brutex/cli.py", "sweep_report"): 1,
}

#: Seam calls sanctioned to OMIT the stamper kwarg entirely — EMPTY by design:
#: every production seam call must STATE its binding (a real stamper, or a
#: comment-justified literal None). Adding an entry here is a conscious,
#: reviewed decision, never a refactor side-effect.
_ALLOWED_OMITTED: Final[dict[tuple[str, str], int]] = {}

#: The pragma-no-cover production ENTRY-POINT binds that MUST stay bound to a
#: non-``None``-literal expression, with exact per-(module, callee) counts:
#: cli.py — cmd_sweep_all's parallel + supervised grids, its stage-3 pass AND
#: the sweep-verify recount's stage-3 pass (2), the recount's shadow
#: run_sweep_grid, the automatic cascade, cmd_cascade's manifest replay;
#: app.py — the dashboard cascade route.
_REQUIRED_BOUND_ENTRYPOINTS: Final[dict[tuple[str, str], int]] = {
    ("brutex/cli.py", "run_parallel_grid"): 1,
    ("brutex/cli.py", "run_supervised_grid"): 1,
    ("brutex/cli.py", "run_stage3_pass"): 2,
    ("brutex/cli.py", "run_sweep_grid"): 1,
    ("brutex/cli.py", "run_and_write_from_specs"): 1,
    ("brutex/cli.py", "run_and_write_from_manifest"): 1,
    ("brutex/dashboard/app.py", "run_and_write_from_manifest"): 1,
}

#: The entry-point modules whose BOUND seam calls the positive law enumerates.
_ENTRYPOINT_MODULES: Final[frozenset[str]] = frozenset({"brutex/cli.py", "brutex/dashboard/app.py"})

#: The VIX-lake-reading stamper BUILDERS (each call is one full lake read).
_BUILDER_CALLEES: Final[frozenset[str]] = frozenset(
    {"build_vix_stamper", "vix_stamper_for_manifest"}
)

#: EXACT builder-call counts per production module — the once-per-launch lake
#: read law (round-3 panel MINOR: round-1 CONFIRMED the VIX lake read TWICE
#: per ``sweep-all --options-sim`` launch, fixed by deleting the extra builds
#: — but nothing pinned the count, so the regression class could silently
#: return). A NEW eager build anywhere in ``brutex/`` fails this map; the
#: complementary DYNAMIC net (a build drifting INSIDE a per-tuple loop keeps
#: the static count at 1) is the exactly-one
#: ``reference_wire.vix_stamper_built`` log-event assertion over the REAL
#: ``cmd_sweep_all`` launch in
#: ``tests/integration/test_cli_sweep_all_vix_stamp.py``.
_REQUIRED_BUILDER_CALLS: Final[dict[tuple[str, str], int]] = {
    # cmd_sweep_all's single launch build + the sweep-verify recount's build.
    ("brutex/cli.py", "build_vix_stamper"): 2,
    # cmd_cascade's manifest replay.
    ("brutex/cli.py", "vix_stamper_for_manifest"): 1,
    # The dashboard cascade route.
    ("brutex/dashboard/app.py", "vix_stamper_for_manifest"): 1,
    # vix_stamper_for_manifest's own delegation to the ONE builder.
    ("brutex/engine/cascade_wire.py", "build_vix_stamper"): 1,
    # The lazy vix_stamper_provider closure (no production consumer today).
    ("brutex/engine/reference_wire.py", "build_vix_stamper"): 1,
}


class _SeamCall(NamedTuple):
    """One seam call site: where it is + how it binds the stamper kwarg."""

    module: str  # repo-relative path
    callee: str
    lineno: int
    literal_none: bool  # reference_stamper=None / stamper_for=None literal
    bound: bool  # a seam kwarg present with a NON-None-literal value
    omitted: bool  # no seam kwarg at all (falls back to the default None)
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
            seam_kwargs = [kw for kw in node.keywords if kw.arg in _SEAM_KWARGS]
            literal_none = any(
                isinstance(kw.value, ast.Constant) and kw.value.value is None for kw in seam_kwargs
            )
            has_splat = any(kw.arg is None for kw in node.keywords)
            calls.append(
                _SeamCall(
                    module=rel,
                    callee=callee,
                    lineno=node.lineno,
                    literal_none=literal_none,
                    bound=bool(seam_kwargs) and not literal_none,
                    omitted=not seam_kwargs,
                    has_splat=has_splat,
                )
            )
    return calls


def _tally(calls: list[_SeamCall]) -> dict[tuple[str, str], int]:
    """Count calls per ``(module, callee)`` key."""
    tally: dict[tuple[str, str], int] = {}
    for call in calls:
        key = (call.module, call.callee)
        tally[key] = tally.get(key, 0) + 1
    return tally


class TestVixStamperBindingPin:
    def test_seam_calls_are_actually_scanned(self) -> None:
        """The scanner sees the production seam surface (a moved/renamed seam
        must update this pin consciously, never silently escape it)."""
        calls = _iter_seam_calls()
        assert len(calls) >= 30, f"expected a wide seam surface, saw {len(calls)}"

    def test_literal_none_only_at_the_sanctioned_sites_with_exact_counts(self) -> None:
        literal_calls = [call for call in _iter_seam_calls() if call.literal_none]
        offenders = [
            f"{call.module}:{call.lineno} {call.callee}(…=None)"
            for call in literal_calls
            if (call.module, call.callee) not in _ALLOWED_LITERAL_NONE
        ]
        assert not offenders, (
            "literal reference_stamper=None outside the sanctioned "
            f"discarded-stream sites: {offenders} — bind the run's stamper (or add "
            "an honest-envelope comment + extend the sanctioned map via a conscious "
            "review, never silently)"
        )
        # EXACT counts: every sanctioned literal must EXIST (removing one —
        # e.g. deleting the explicit None while leaving the default — silently
        # converts a stated decision into an omission) and must not multiply.
        assert _tally(literal_calls) == _ALLOWED_LITERAL_NONE

    def test_no_seam_call_omits_the_stamper_kwarg_outside_the_enumerated_map(self) -> None:
        """Omission = the default ``None`` with no stated decision — the exact
        silent-re-dormanting a refactor produces by deleting one kwarg. The
        round-2 panel found three shipped omissions this law would have
        caught; the map is EMPTY today and any new entry is a conscious
        review outcome."""
        omitted_calls = [call for call in _iter_seam_calls() if call.omitted]
        offenders = [
            f"{call.module}:{call.lineno} {call.callee}(<no stamper kwarg>)"
            for call in omitted_calls
            if (call.module, call.callee) not in _ALLOWED_OMITTED
        ]
        assert not offenders, (
            f"seam calls silently OMIT the stamper kwarg: {offenders} — bind the "
            "run's stamper, or state literal reference_stamper=None with an "
            "honest-envelope comment (and extend the sanctioned literal map), or "
            "enumerate the omission in _ALLOWED_OMITTED via a conscious review"
        )
        assert _tally(omitted_calls) == _ALLOWED_OMITTED

    def test_entrypoint_binds_are_present_with_exact_counts(self) -> None:
        """The positive-presence law over the pragma-no-cover entry points:
        every enumerated (module, callee) must carry its exact count of
        NON-None bindings — deleting `reference_stamper=reference_stamper` at
        a launch site fails HERE even with zero shim coverage."""
        bound_entrypoint_calls = [
            call for call in _iter_seam_calls() if call.bound and call.module in _ENTRYPOINT_MODULES
        ]
        assert _tally(bound_entrypoint_calls) == _REQUIRED_BOUND_ENTRYPOINTS, (
            "the production entry-point binds drifted from the enumerated map — "
            "a MISSING key means a launch site lost its stamper bind (the "
            "#935/#1014 dormant-binding trap); an EXTRA key means a new "
            "production bind landed and must be enumerated consciously"
        )

    def test_builder_calls_match_the_once_per_launch_map_exactly(self) -> None:
        """The once-per-launch VIX-lake-read law (round-3 panel): every call
        to a stamper BUILDER in production code is enumerated with an exact
        count — a second eager build in a launcher (the round-1 confirmed
        double-read class) or a brand-new build site fails here and forces a
        conscious review. (A build MOVED inside a per-tuple loop keeps the
        static count — that arm is owned by the exactly-one
        ``vix_stamper_built`` log assertion in the sweep-all integration
        suite.)"""
        package_root = Path(brutex.__file__).resolve().parent
        tally: dict[tuple[str, str], int] = {}
        for path in sorted(package_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel = str(path.relative_to(package_root.parent))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                callee = _callee_name(node)
                if callee in _BUILDER_CALLEES:
                    key = (rel, callee)
                    tally[key] = tally.get(key, 0) + 1
        assert tally == _REQUIRED_BUILDER_CALLS, (
            "the production stamper-builder call sites drifted from the "
            "once-per-launch map — an EXTRA key/count means a new lake-reading "
            "build landed (enumerate it consciously, never a second build per "
            "launch); a MISSING key means a launcher lost its single build"
        )

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
