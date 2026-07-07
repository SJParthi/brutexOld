"""Bind-capture tests for the cascade VIX-stamper entry points (DEC-VIX-STAMP-WIRE-001).

Round-2 panel MAJOR: ``cmd_cascade`` and the dashboard ``/api/v1/cascade/run``
route are ``# pragma: no cover`` shims whose ``reference_stamper`` binds had
ZERO executing coverage — a deleted kwarg kept every gate green. These tests
EXECUTE both shims with the heavy lake/manifest internals monkeypatched at
their source module (both shims import them locally at call time), capturing
the exact seam crossing:

* the manifest-derived stamper (``vix_stamper_for_manifest`` over the resolved
  lake root) is the object handed to ``run_and_write_from_manifest`` — not a
  default ``None``;
* the options-sim helpers build NO stamper at all after the round-2 fix (their
  stamps were provably discarded at the SpotSignal conversion — the sanctioned
  literal-``None`` site in ``_replay_spot_signals_for_row``), so the wasted
  per-invocation VIX lake read is pinned gone.

The static twin lives in ``tests/unit/test_vix_stamper_binding_pin.py`` (the
positive-presence entry-point map); the real-lake end-to-end twin in
``tests/integration/test_cli_sweep_all_vix_stamp.py``.
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


def _patch_cascade_seams(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any], sentinel: object
) -> None:
    """Patch the cascade internals at their source module (call-time imports)."""
    manifest = SimpleNamespace(sweep_id="cafe" * 4)
    monkeypatch.setattr(cascade_wire, "load_manifest", lambda root: manifest)
    monkeypatch.setattr(
        cascade_wire,
        "load_bars_for_manifest",
        lambda *args, **kwargs: lambda _i, _t: [],
    )

    def _fake_stamper_for_manifest(mani: Any, **kwargs: Any) -> object:
        captured["stamper_manifest"] = mani
        captured["stamper_kwargs"] = kwargs
        return sentinel

    def _fake_run(root: Path, **kwargs: Any) -> SimpleNamespace:
        captured["run_root"] = root
        captured["run_kwargs"] = kwargs
        return _stub_report()

    monkeypatch.setattr(cascade_wire, "vix_stamper_for_manifest", _fake_stamper_for_manifest)
    monkeypatch.setattr(cascade_wire, "run_and_write_from_manifest", _fake_run)


class TestCmdCascadeBind:
    def test_cmd_cascade_binds_the_manifest_stamper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        sentinel = object()
        _patch_cascade_seams(monkeypatch, captured, sentinel)

        rc = cli.cmd_cascade(lake_root=tmp_path, results_root=None, catalog_path=None, run_id=None)

        assert rc == 0
        # The stamper is built FROM the manifest over the resolved lake …
        assert captured["stamper_manifest"].sweep_id == "cafe" * 4
        assert captured["stamper_kwargs"]["lake_root"] == tmp_path
        # … and that exact object crosses the seam — never a defaulted None.
        assert captured["run_kwargs"]["reference_stamper"] is sentinel
        assert captured["run_root"] == tmp_path / "sweeps"


class TestDashboardCascadeRouteBind:
    def test_api_cascade_run_binds_the_manifest_stamper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("fastapi")
        from brutex.dashboard.app import DashboardState, build_app
        from fastapi.testclient import TestClient

        captured: dict[str, Any] = {}
        sentinel = object()
        _patch_cascade_seams(monkeypatch, captured, sentinel)

        store = SimpleNamespace(path=tmp_path / "instruments.duckdb")
        app = build_app(DashboardState(), instrument_store=store)
        with TestClient(app) as client:
            resp = client.post("/api/v1/cascade/run")

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # The route derives the lake from the wired store and binds the
        # manifest stamper into the replay — never a defaulted None.
        assert captured["stamper_kwargs"]["lake_root"] == tmp_path
        assert captured["run_kwargs"]["reference_stamper"] is sentinel
        assert captured["run_root"] == tmp_path / "sweeps"


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
