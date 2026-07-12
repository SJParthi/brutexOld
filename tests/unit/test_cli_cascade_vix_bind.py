"""No-stamper-build pins for the cascade replay entry points (DEC-VIX-STAMP-WIRE-001).

Round-6 review: the manual cascade replays (``cmd_cascade`` and the dashboard
``/api/v1/cascade/run`` route) each built a manifest-window VIX stamper via
``vix_stamper_for_manifest`` — a full window-bounded VIX lake read per
replay/click — to stamp trades that are provably DISCARDED at the
``CandidateInput`` scalar conversion (the tree's own contract: the cascade
report bytes are identical with or without a stamper). That is the exact
dead-build class the round-2 review deleted at the two options-sim sites, so
the round-6 fix deleted the builder AND the whole cascade stamper seam; the
sanctioned literal-``None`` now lives at the ``simulate_trades`` call inside
``build_candidate_input`` (AST-pinned).

These tests EXECUTE both ``# pragma: no cover`` shims with the heavy
lake/manifest internals monkeypatched at their source module (both shims
import them locally at call time), pinning the seam crossing:

* neither shim builds ANY stamper (the wasted per-replay VIX lake read is
  gone), and neither passes a stamper kwarg into
  ``run_and_write_from_manifest`` — the callable no longer accepts one;
* the options-sim helpers likewise carry no stamper build (the round-2 fix,
  kept pinned here).

The static twin lives in ``tests/unit/test_vix_stamper_binding_pin.py`` (the
builder-call exact-count law — a resurrected ``vix_stamper_for_manifest``
call anywhere in ``brutex/`` fails it); the engine-side twin in
``tests/unit/test_engine_vix_stamper_wiring.py::TestCascadeStreamsUnstamped``.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from brutex import cli
from brutex.engine import cascade_wire


def _stub_report() -> SimpleNamespace:
    return SimpleNamespace(groups={}, n_input=0, n_survived=0)


def _patch_cascade_seams(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    """Patch the cascade internals at their source module (call-time imports)."""
    manifest = SimpleNamespace(sweep_id="cafe" * 4)
    monkeypatch.setattr(cascade_wire, "load_manifest", lambda root: manifest)
    monkeypatch.setattr(
        cascade_wire,
        "load_bars_for_manifest",
        lambda *args, **kwargs: lambda _i, _t: [],
    )

    def _fake_run(root: Path, **kwargs: Any) -> SimpleNamespace:
        captured["run_root"] = root
        captured["run_kwargs"] = kwargs
        return _stub_report()

    monkeypatch.setattr(cascade_wire, "run_and_write_from_manifest", _fake_run)


class TestCmdCascadeBuildsNoStamper:
    def test_cmd_cascade_replays_without_any_stamper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        _patch_cascade_seams(monkeypatch, captured)

        rc = cli.cmd_cascade(lake_root=tmp_path, results_root=None, catalog_path=None, run_id=None)

        assert rc == 0
        assert captured["run_root"] == tmp_path / "sweeps"
        # The replay crosses the seam WITHOUT any stamper kwarg — the dead
        # per-replay VIX lake read is gone, not merely defaulted.
        assert "reference_stamper" not in captured["run_kwargs"]

    def test_cmd_cascade_carries_no_stamper_build(self) -> None:
        source = inspect.getsource(cli.cmd_cascade)
        assert "build_vix_stamper" not in source
        # (mentioned only in the honest-envelope comment, never called)
        assert "vix_stamper_for_manifest(" not in source


class TestDashboardCascadeRouteBuildsNoStamper:
    def test_api_cascade_run_replays_without_any_stamper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("fastapi")
        from brutex.dashboard.app import DashboardState, build_app
        from fastapi.testclient import TestClient

        captured: dict[str, Any] = {}
        _patch_cascade_seams(monkeypatch, captured)

        store = SimpleNamespace(path=tmp_path / "instruments.duckdb")
        app = build_app(DashboardState(), instrument_store=store)
        with TestClient(app) as client:
            resp = client.post("/api/v1/cascade/run")

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert captured["run_root"] == tmp_path / "sweeps"
        assert "reference_stamper" not in captured["run_kwargs"]


class TestOptionsSimBuildsNoStamper:
    def test_options_sim_helpers_carry_no_stamper_build(self) -> None:
        """The round-2 fix REMOVED the two provably-wasted stamper builds.

        The replay's stamps never reached any output
        (``signals_from_spot_trades`` reads only entry/exit/direction), so
        the builds cost a full window-bounded VIX lake read per invocation —
        one of them even ran for ZERO winners. The sanctioned literal-None
        now lives at the ``simulate_trades`` call inside
        ``_replay_spot_signals_for_row`` (AST-pinned); this source pin keeps
        the dead builds from silently returning without the leaderboard
        actually surfacing VIX.
        """
        for fn in (
            cli.cmd_options_sim,
            cli._maybe_run_options_sim_on_winners,
            cli._options_sim_for_row,
            cli._replay_spot_signals_for_row,
        ):
            assert "build_vix_stamper" not in inspect.getsource(fn)
