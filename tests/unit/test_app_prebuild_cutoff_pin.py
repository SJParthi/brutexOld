"""Route-arm regression pin — the /pull-pipeline prebuild cutoff stays the PINNED INSTANT.

Hostile-review round-4 MAJOR (the coverage-honesty lens) on the #1551-#1561
polish pack: the #1401 M1 regression — an explicit today-IST **DATE** pin
(``daily_before=<date>``) in the ``/pull-pipeline`` job body — left
DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001 production-dead, and it shipped
UNDETECTED because the route's ``_work`` closure rides the pre-existing
``# pragma: no cover — runs on the job thread`` shield: no executing test can
see the cutoff wiring there. The #1552 repair (``prebuild_cutoff_now =
now_ist()`` threaded as ``daily_cutoff_now=`` into BOTH the sequential
``_materialize`` closure and ``run_pipeline_prebuild_guarded``) was pinned by
executing tests at the orchestrator + materialize layers — but the ROUTE arm
itself had NO pin, so re-introducing the exact #1401 edit would pass the whole
suite again.

This test closes that recurrence surface MECHANICALLY (the #1492-G3
``resample_bars`` AST-pin precedent — the sanctioned tool for asserting facts
about code that executing tests cannot reach): an AST walk over
``brutex/dashboard/app.py`` locates the ONE ``_work`` closure that calls
``run_pipeline_prebuild_guarded`` and asserts the DEC-DAILY-CUTOFF-GROUNDED-001
route contract:

* the forming-session cutoff is pinned as ONE ``now_ist()`` INSTANT per run
  (exactly one such call in the whole closure — never a per-call clock, which
  would break the M1 cross-midnight byte-identity);
* BOTH downstream arms — the sequential ``materialize_lake_timeframes``
  closure AND the ``run_pipeline_prebuild_guarded`` call — receive THAT SAME
  pinned name as ``daily_cutoff_now=`` (one instant, both paths);
* NEITHER passes ``daily_before=`` (an explicit DATE is LAW at the
  materialize layer — it would suppress the session-close-aware default AND
  the dense symbols' data-grounded arm: the exact #1401 regression shape);
* no call ANYWHERE in ``app.py`` passes ``daily_before=`` (the route file has
  no sanctioned explicit-date site — lake-heal/doctor explicit cutoffs live in
  their own modules).

Defense-in-depth honesty (Golden Rule #10): the layers BELOW the route
(``materialize_lake_timeframes``'s session-close-aware default and
``run_pipeline_prebuild_parallel``'s own instant pin) already bound the worst
silent regression to a wrong 1day set for a day — this pin exists so the
route arm cannot silently drift back to the #1401 shape at all.
"""

from __future__ import annotations

import ast
from pathlib import Path

import brutex

_APP_PATH = Path(brutex.__file__).resolve().parent / "dashboard" / "app.py"

_GUARDED = "run_pipeline_prebuild_guarded"
_MATERIALIZE = "materialize_lake_timeframes"
_CLOCK = "now_ist"
_FORBIDDEN_KW = "daily_before"
_PIN_KW = "daily_cutoff_now"


def _called_name(node: ast.Call) -> str | None:
    """The simple callee name of *node* (``f(...)`` or ``m.f(...)``), else None."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _calls_to(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node for node in ast.walk(tree) if isinstance(node, ast.Call) and _called_name(node) == name
    ]


def _keyword(node: ast.Call, name: str) -> ast.keyword | None:
    for kw in node.keywords:
        if kw.arg == name:
            return kw
    return None


def _prebuild_work() -> ast.FunctionDef:
    """The ONE ``_work`` closure in app.py that calls the prebuild guard."""
    tree = ast.parse(_APP_PATH.read_text(encoding="utf-8"))
    candidates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_work" and _calls_to(node, _GUARDED)
    ]
    assert len(candidates) == 1, (
        f"expected exactly ONE _work closure calling {_GUARDED} in app.py, "
        f"found {len(candidates)} — the /pull-pipeline route moved; update this "
        "pin consciously (DEC-DAILY-CUTOFF-GROUNDED-001)"
    )
    return candidates[0]


class TestPullPipelinePrebuildCutoffPin:
    """The pragma-shielded route arm's cutoff wiring, pinned at the AST level."""

    def test_route_pins_one_instant_and_threads_it_to_both_arms(self) -> None:
        work = _prebuild_work()

        # (1) EXACTLY ONE now_ist() call in the whole closure — the pinned
        # instant. A second call would reopen the per-call clock-skew hole the
        # M1 pin closed (two paths deriving different 1day sets across a
        # midnight-IST / session-close boundary).
        clock_calls = _calls_to(work, _CLOCK)
        assert len(clock_calls) == 1, (
            f"expected exactly ONE {_CLOCK}() pin in the prebuild _work, found "
            f"{len(clock_calls)} — the cutoff must be pinned ONCE per run"
        )
        pinned = clock_calls[0]
        assert not pinned.args and not pinned.keywords  # a bare clock read

        # (2) The pin is a plain single-name assignment: NAME = now_ist().
        assigns = [
            node for node in ast.walk(work) if isinstance(node, ast.Assign) and node.value is pinned
        ]
        assert len(assigns) == 1, (
            "the now_ist() pin must be bound to a name (NAME = now_ist()) so "
            "both arms can share the ONE instant"
        )
        (target,) = assigns[0].targets
        assert isinstance(target, ast.Name), "the cutoff pin must bind a simple name"
        pin_name = target.id

        # (3) The sequential arm: materialize_lake_timeframes(...,
        # daily_cutoff_now=<pin>, include_daily=True) and NEVER daily_before=.
        materialize_calls = _calls_to(work, _MATERIALIZE)
        assert len(materialize_calls) == 1, (
            f"expected exactly ONE {_MATERIALIZE} call in the prebuild _work, "
            f"found {len(materialize_calls)}"
        )
        # (4) The parallel/guard arm: run_pipeline_prebuild_guarded(...,
        # daily_cutoff_now=<pin>) and NEVER daily_before=.
        guarded_calls = _calls_to(work, _GUARDED)
        assert len(guarded_calls) == 1

        for call in (*materialize_calls, *guarded_calls):
            callee = _called_name(call)
            assert _keyword(call, _FORBIDDEN_KW) is None, (
                f"{callee} passes {_FORBIDDEN_KW}= at the /pull-pipeline route — "
                "the EXACT #1401 M1 regression: an explicit DATE is LAW at the "
                "materialize layer and suppresses the session-close-aware "
                "default AND the data-grounded arm "
                "(DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001 / "
                "DEC-DAILY-CUTOFF-GROUNDED-001)"
            )
            # No **kwargs smuggling past this pin.
            assert all(kw.arg is not None for kw in call.keywords), (
                f"{callee} uses **kwargs at the route — the cutoff pin cannot "
                "be verified through a dynamic mapping; pass keywords explicitly"
            )
            pin_kw = _keyword(call, _PIN_KW)
            assert pin_kw is not None, (
                f"{callee} no longer passes {_PIN_KW}= at the /pull-pipeline "
                "route — the route arm of DEC-DAILY-CUTOFF-GROUNDED-001 lost "
                "its pinned instant"
            )
            assert isinstance(pin_kw.value, ast.Name) and pin_kw.value.id == pin_name, (
                f"{callee}'s {_PIN_KW}= must be the SHARED pinned name "
                f"{pin_name!r} (one instant, both paths) — an inline clock call "
                "or a different name breaks the M1 one-instant law"
            )

        # (5) include_daily=True on the sequential arm (the daily derive is the
        # whole point of the cutoff — dropping it would dead-code the pin).
        include_daily = _keyword(materialize_calls[0], "include_daily")
        assert include_daily is not None
        assert isinstance(include_daily.value, ast.Constant)
        assert include_daily.value.value is True

    def test_no_daily_before_keyword_anywhere_in_app(self) -> None:
        """No call in app.py passes ``daily_before=`` — the route file has no
        sanctioned explicit-date cutoff site (lake-heal / doctor explicit
        cutoffs live in their own modules); any appearance here is the #1401
        regression shape resurfacing."""
        tree = ast.parse(_APP_PATH.read_text(encoding="utf-8"))
        offenders = [
            f"line {node.lineno}: {_called_name(node)}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _keyword(node, _FORBIDDEN_KW) is not None
        ]
        assert offenders == [], (
            f"app.py passes {_FORBIDDEN_KW}= at {offenders} — an explicit DATE "
            "cutoff at the route is LAW downstream and regresses "
            "DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001 (#1401 M1)"
        )
