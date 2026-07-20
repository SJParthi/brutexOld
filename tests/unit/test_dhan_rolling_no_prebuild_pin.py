"""Dhan rolling options are 1-MINUTE-NATIVE — the NO-PREBUILD absence pins (the teeth).

DEC-DHAN-OPTIONS-NO-PREBUILD-001 (MIGRATION.md #1803, implementing
DEC-SCOPE-MINIMAL-CORE-001 — the operator-authorized scope decision) +
DEC-DHAN-NO-FUTURES-001 (MIGRATION.md #1802): on the
Dhan feed, derived-timeframe materialization is SPOT-ONLY — the rolling-ATM
option track is stored + served 1-minute-native BY DESIGN (option fills resolve
on the next 1-minute bar worst-case, so a derived option TF is cost without
benefit; Dhan has no futures leg — its derivative history is rolling ATM±10
expired options only). These pins make the ABSENCE structural:

1. **Behavior pin** — a happy-path rolling pull (the existing fake-client
   fixtures) leaves ONLY ``1minute/`` interval dirs under
   ``feeds/dhan/rolling_options/``; no dir anywhere in the tree carries a
   derived-TF lake label (the canonical ``LAKE_INTERVAL_LABELS`` vocabulary).
2. **AST pin** — the rolling pull production modules (the dashboard rolling
   module + the rolling spine + every ``rolling_option_*`` module + the rolling
   ``/db`` view), the cli.py Dhan arms AND the app.py rolling ROUTE functions
   (per-function scan — never whole-module, the spot launcher legitimately
   imports materialize machinery) import NO materialize/prebuild symbol
   (the ``test_gpu_gate_drift_pin.py`` / vix-stamper-binding AST-pin idiom).
3. **Spot stays** — the pin cannot be satisfied by deleting the SPOT prebuild:
   ``brutex/dashboard/dhan_pull.py`` must still import
   ``materialize_lake_timeframes``, define ``run_dhan_prebuild``, and reference
   it from ``default_dhan_spot_pull``; a REAL spot prebuild over seeded 1-minute
   bars must still materialise intraday derived TFs. Since
   DEC-DHAN-NATIVE-DAILY-001 (MIGRATION.md #1867) the intraday-only law is
   STRUCTURAL: ``run_dhan_prebuild`` exposes NO ``include_daily`` parameter
   (``include_daily=False`` is hardcoded at its materialize call site — the
   Dhan ``1day`` partition holds NATIVE vendor daily bars, never a 1-minute
   derivation), so these pins assert the kwarg's ABSENCE + the no-``1day``
   derivation instead of the base branch's 1-day-AGNOSTIC
   ``include_daily=False`` call.
4. **Lock isolation pin** — the spot prebuild's derive lock keys on the DHAN
   FEED root (``feeds/dhan/.locks/derive.lock``), never the primary lake root
   (a primary-root key would serialize Groww + Dhan prebuilds on one lock).

Honest envelope (the GPU-pin residual class): static imports are AST-detected;
a COMPUTED import (``importlib.import_module("brutex.data." + name)``) is not —
the literal import shapes are the ones a real prebuild-wiring PR writes. The
behavior pin (1) catches a prebuild that runs in the PULL's happy path; a
flag-gated default-off / route-level / computed-import prebuild is the
disclosed residual (the AST pins over the rolling modules + the rolling route
handler + the cli Dhan arms are the net for the route-level shapes).
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from brutex.adapters.dhan.rolling_option_adapter import RollingOptionAdapter
from brutex.adapters.dhan.rolling_option_plan import enumerate_rolling_option_keys
from brutex.dashboard.dhan_pull import DhanPrebuildTarget, run_dhan_prebuild
from brutex.dashboard.dhan_rolling_pull import run_dhan_rolling_option_pull
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.timeframes import LAKE_INTERVAL_LABELS

#: Frozen IST "today" — the unified rolling-options floor resolves 2022-06-01
#: here (DEC-DHAN-ROLLING-FLOOR-UNIFY-001), below the fixture 2024 ranges.
_FROZEN_TODAY = date(2026, 7, 15)

#: The forbidden prebuild/materialize import surface for the ROLLING modules —
#: the module that owns derived-TF materialization, the Groww pipeline/gated
#: prebuild orchestration modules, plus the prebuild entry-point names (Dhan
#: spot + pipeline/gated) a mis-wiring would reach for.
_FORBIDDEN_MODULES = frozenset(
    {
        "brutex.data.timeframe_materialize",
        "brutex.dashboard.pipeline_prebuild",
        "brutex.dashboard.gated_prebuild",
    }
)
_FORBIDDEN_NAMES = frozenset(
    {
        "materialize_lake_timeframes",
        "materialize_timeframes",
        "run_dhan_prebuild",
        "run_pipeline_prebuild",
        "run_pipeline_prebuild_parallel",
        "run_pipeline_prebuild_guarded",
        "run_gated_prebuild",
        "PrebuildKind",
        "PrebuildOutcome",
        "MaterializeResult",
    }
)

_REPO = Path(__file__).resolve().parents[2]

#: The rolling-options PRODUCTION modules (the pull path end-to-end): the
#: dashboard rolling module, the rolling spine, every rolling_option_* adapter
#: module, and the rolling /db view.
_ROLLING_MODULES = (
    _REPO / "brutex" / "dashboard" / "dhan_rolling_pull.py",
    _REPO / "brutex" / "dashboard" / "dhan_rolling_db_view.py",
    _REPO / "brutex" / "adapters" / "dhan" / "run_dhan_rolling_option_ingest.py",
    *sorted((_REPO / "brutex" / "adapters" / "dhan").glob("rolling_option_*.py")),
)

_PAYLOAD: dict[str, list[object]] = {
    "timestamp": [1_717_386_300.0],
    "open": [100],
    "high": [102],
    "low": [99],
    "close": [101],
}


class _FakeClient:
    """A rolling-option client returning one fixed bar payload (the house fake)."""

    def rolling_option_data(self, *, body: object) -> dict[str, list[object]]:
        _ = body
        return _PAYLOAD


def _forbidden_imports(tree: ast.AST) -> list[str]:
    """Return every forbidden prebuild/materialize import found in *tree*."""
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name
                if root in _FORBIDDEN_MODULES or root.split(".")[-1] in _FORBIDDEN_NAMES:
                    hits.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in _FORBIDDEN_MODULES:
                hits.append(f"from {module} import …")
            for alias in node.names:
                if alias.name in _FORBIDDEN_NAMES:
                    hits.append(f"from {module} import {alias.name}")
    return hits


# ── 1. behavior pin: a rolling pull leaves ONLY 1minute interval dirs ─────────


def test_rolling_pull_happy_path_writes_only_1minute_interval_dirs(tmp_path: Path) -> None:
    keys = enumerate_rolling_option_keys(band=0)  # 48 keys — every selector arm
    report = run_dhan_rolling_option_pull(
        lake_root=tmp_path,
        adapter=RollingOptionAdapter(client=_FakeClient()),
        keys=keys,
        start=date(2024, 6, 1),
        end=date(2024, 6, 15),
        today=_FROZEN_TODAY,
    )
    assert report.total_bars == len(keys)  # the happy path genuinely wrote bars
    rolling_root = tmp_path / "feeds" / "dhan" / "rolling_options"
    assert rolling_root.is_dir()
    parquet_files = sorted(rolling_root.rglob("*.parquet"))
    assert parquet_files, "the pull must have written partitions"
    # The interval segment of every partition path (…/<interval>/<yyyy>/<mm>.parquet)
    # is EXACTLY "1minute" — 1-minute-native, no derived-TF partition anywhere.
    interval_segments = {p.parts[-3] for p in parquet_files}
    assert interval_segments == {"1minute"}
    # NO directory anywhere in the rolling tree carries a derived-TF lake label.
    derived_labels = set(LAKE_INTERVAL_LABELS.values()) - {"1minute"}
    all_dir_names = {d.name for d in rolling_root.rglob("*") if d.is_dir()}
    assert all_dir_names & derived_labels == set(), (
        f"derived-TF dirs leaked into the rolling tree: {all_dir_names & derived_labels}"
    )
    assert "1minute" in all_dir_names


# ── 2. AST pin: the rolling pull path imports NO prebuild/materialize symbol ──


def test_rolling_modules_import_no_prebuild_symbol() -> None:
    assert _ROLLING_MODULES, "the rolling module list must not be empty"
    for module_path in _ROLLING_MODULES:
        assert module_path.is_file(), f"missing rolling module: {module_path}"
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        hits = _forbidden_imports(tree)
        assert hits == [], f"{module_path.name} imports prebuild machinery: {hits}"


def _named_functions(tree: ast.AST, wanted: frozenset[str]) -> dict[str, ast.AST]:
    """Find the *wanted* (async or sync, possibly nested) function nodes in *tree*."""
    found: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in wanted:
            found[node.name] = node
    return found


def test_cli_dhan_arms_import_no_prebuild_symbol() -> None:
    # The cli.py Dhan arms use function-local imports — scan exactly those bodies.
    cli_tree = ast.parse((_REPO / "brutex" / "cli.py").read_text(encoding="utf-8"))
    wanted = frozenset({"cmd_fetch_dhan", "_run_dhan_fetch", "cmd_enrich_greeks_dhan"})
    found = _named_functions(cli_tree, wanted)
    assert set(found) == set(wanted), f"cli Dhan arms missing/renamed: {set(wanted) - set(found)}"
    for name, fn in found.items():
        hits = _forbidden_imports(fn)
        assert hits == [], f"cli.{name} imports prebuild machinery: {hits}"


def test_rolling_route_functions_import_no_prebuild_symbol() -> None:
    """The rolling ROUTE path in app.py imports no prebuild machinery — scanned
    PER FUNCTION (the cli-arm idiom), deliberately NEVER whole-module: app.py
    legitimately imports materialize machinery for the SPOT launcher."""
    app_tree = ast.parse((_REPO / "brutex" / "dashboard" / "app.py").read_text(encoding="utf-8"))
    wanted = frozenset(
        {
            "_launch_dhan_rolling_pipeline",
            "api_universe_pull_rolling_options_dhan",
            "_default_dhan_rolling_option_pull_production",
        }
    )
    found = _named_functions(app_tree, wanted)
    assert set(found) == set(wanted), (
        f"rolling route functions missing/renamed: {set(wanted) - set(found)}"
    )
    for name, fn in found.items():
        hits = _forbidden_imports(fn)
        assert hits == [], f"app.{name} imports prebuild machinery: {hits}"


# ── 3. spot stays: the pin cannot be satisfied by deleting the SPOT prebuild ──


def test_spot_module_still_imports_and_wires_the_prebuild() -> None:
    spot_path = _REPO / "brutex" / "dashboard" / "dhan_pull.py"
    tree = ast.parse(spot_path.read_text(encoding="utf-8"))
    hits = _forbidden_imports(tree)
    assert any("materialize_lake_timeframes" in h for h in hits), (
        "the SPOT module must keep importing materialize_lake_timeframes "
        "(deleting the spot prebuild would vacuously satisfy the rolling pins)"
    )
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "run_dhan_prebuild" in fns, "the SPOT prebuild entry point must stay"
    seam = fns["default_dhan_spot_pull"]
    called = {
        n.id for n in ast.walk(seam) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    assert "run_dhan_prebuild" in called, "default_dhan_spot_pull must still wire the spot prebuild"


def _seed_dhan_spot_1m(dhan_root: Path, *, symbol: str = "NIFTY", n_bars: int = 90) -> None:
    """Seed one session of REAL 1-minute index bars under the Dhan FEED root."""
    ist = UTC  # bars are stored UTC; 03:45 UTC == 09:15 IST session open
    base = datetime(2024, 6, 3, 3, 45, tzinfo=ist)
    bars = []
    for i in range(n_bars):
        close = Decimal("100.00") + Decimal(i) / 10
        bars.append(
            CandleBar(
                timestamp=base + timedelta(minutes=i),
                open=close - Decimal("0.05"),
                high=close + Decimal("0.05"),
                low=close - Decimal("0.10"),
                close=close,
                volume=1000,
                open_interest=None,
            )
        )
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=dhan_root))
    sink.append(exchange="NSE", segment="INDEX", symbol=symbol, interval="1minute", bars=bars)


def test_spot_prebuild_signature_has_no_include_daily_parameter() -> None:
    """DEC-DHAN-NATIVE-DAILY-001 (MIGRATION.md #1867) made the intraday-only
    flip STRUCTURAL: ``run_dhan_prebuild`` must expose NO ``include_daily``
    parameter (the False is hardcoded at the materialize call site, so no
    caller can ever re-derive the native-daily ``1day`` partition). This pin
    fails loudly if the kwarg silently comes back."""
    params = inspect.signature(run_dhan_prebuild).parameters
    assert "include_daily" not in params, (
        "run_dhan_prebuild grew an include_daily parameter back — the "
        "DEC-DHAN-NATIVE-DAILY-001 structural intraday-only contract is broken"
    )


def test_spot_prebuild_still_materialises_intraday_tfs_for_real(tmp_path: Path) -> None:
    """The REAL (un-faked) spot prebuild over seeded 1-minute bars still derives
    intraday TFs under the Dhan feed root — and NEVER a derived ``1day``
    (structural intraday-only, DEC-DHAN-NATIVE-DAILY-001: the ``1day``
    partition is native vendor daily, so the prebuild must not create it
    from 1-minute)."""
    dhan_root = tmp_path / "feeds" / "dhan"
    _seed_dhan_spot_1m(dhan_root)
    outcome = run_dhan_prebuild(
        dhan_root=dhan_root,
        targets=[DhanPrebuildTarget(exchange="NSE", symbol="NIFTY")],
    )
    assert outcome.failed == {}
    assert outcome.materialized.get("NIFTY", 0) > 0
    symbol_dir = dhan_root / "bars" / "NSE" / "INDEX" / "NIFTY"
    derived_dirs = {d.name for d in symbol_dir.iterdir() if d.is_dir()} - {"1minute"}
    assert "5minute" in derived_dirs, f"no intraday derived TF appeared: {derived_dirs}"
    assert "1day" not in derived_dirs, (
        "the Dhan prebuild derived a 1day partition from 1-minute — the "
        "native-daily law (DEC-DHAN-NATIVE-DAILY-001) forbids it"
    )


# ── 4. lock isolation: the derive lock keys on the DHAN FEED root ─────────────


def test_spot_prebuild_derive_lock_keys_on_dhan_feed_root(tmp_path: Path) -> None:
    """The prebuild's derive lock lives at ``feeds/dhan/.locks/derive.lock`` —
    never the primary lake root (a primary-root key would serialize Groww + Dhan
    spot prebuilds on ONE lock, an isolation bug)."""
    dhan_root = tmp_path / "feeds" / "dhan"
    _seed_dhan_spot_1m(dhan_root)
    run_dhan_prebuild(
        dhan_root=dhan_root,
        targets=[DhanPrebuildTarget(exchange="NSE", symbol="NIFTY")],
    )
    assert (dhan_root / ".locks" / "derive.lock").is_file()
    assert not (tmp_path / ".locks").exists(), (
        "the derive lock must NOT key on the primary lake root"
    )
