"""Tests for :mod:`brutex.dashboard.dhan_rolling_pull` — the rolling-ATM pull core.

DEC-DHAN-ROLLING-OPTIONS-001. The surface glue that drives the Dhan rolling-ATM option
pull into ``feeds/dhan/rolling_options/`` — fully fake-driven (an injected adapter over
a fake client + a temp lake). Also the HONESTY teeth: every label is "rolling-ATM"
(ATM±N per bar), NEVER "per-contract / per-expiry / per-strike".
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path

from brutex.adapters.dhan.rolling_option_adapter import RollingOptionAdapter
from brutex.adapters.dhan.rolling_option_plan import enumerate_rolling_option_keys
from brutex.dashboard.dhan_pull import DhanPullRequest
from brutex.dashboard.dhan_rolling_pull import (
    DhanRollingOptionPullReport,
    default_dhan_rolling_option_pull,
    run_dhan_rolling_option_pull,
)

_PAYLOAD: dict[str, list[object]] = {
    "timestamp": [1_717_386_300.0],
    "open": [100],
    "high": [102],
    "low": [99],
    "close": [101],
}


class _FakeClient:
    """A rolling-option client returning a fixed payload (or empty on demand)."""

    def __init__(self, payload: Mapping[str, object]) -> None:
        self.payload = payload
        self.calls = 0

    def rolling_option_data(self, *, body: Mapping[str, object]) -> Mapping[str, object]:
        _ = body
        self.calls += 1
        return self.payload


def _adapter(payload: Mapping[str, object]) -> RollingOptionAdapter:
    return RollingOptionAdapter(client=_FakeClient(payload))


# ── run_dhan_rolling_option_pull ───────────────────────────────────────────────


def test_run_pull_writes_into_feeds_dhan_rolling_options(tmp_path: Path) -> None:
    keys = enumerate_rolling_option_keys(band=0)  # 3 × 2 × 4 × 1 × 2 = 48 keys
    report = run_dhan_rolling_option_pull(
        lake_root=tmp_path,
        adapter=_adapter(_PAYLOAD),
        keys=keys,
        start=date(2024, 6, 1),
        end=date(2024, 6, 15),
    )
    assert report.market == "dhan-option"
    assert report.total_bars == len(keys)  # 1 bar per key/partition
    assert report.failed_windows == 0
    # Bars land under the NESTED feeds/dhan/rolling_options/ tree — Groww lake untouched.
    rolling_root = tmp_path / "feeds" / "dhan" / "rolling_options"
    assert rolling_root.is_dir()
    assert not (tmp_path / "bars").exists()
    assert not (tmp_path / "options").exists()
    # An honest ATM±N selector path — NO date, NO absolute strike segment.
    nifty_week_ec0 = rolling_root / "NSE" / "NIFTY" / "WEEK" / "EC0" / "ATM" / "CALL" / "1minute"
    assert nifty_week_ec0.is_dir()


def test_run_pull_empty_payload_is_clean_empty(tmp_path: Path) -> None:
    keys = enumerate_rolling_option_keys(band=0)
    report = run_dhan_rolling_option_pull(
        lake_root=tmp_path,
        adapter=_adapter({}),  # EMPTY-FROM-DHAN — clean-empty, never a fabricated bar
        keys=keys,
        start=date(2024, 6, 1),
        end=date(2024, 6, 15),
    )
    assert report.total_bars == 0
    assert report.failed_windows == 0
    assert not (tmp_path / "feeds" / "dhan" / "rolling_options").exists()


# ── default_dhan_rolling_option_pull (the production seam, fake adapter_factory) ─


def test_default_pull_seam_is_fake_driven(tmp_path: Path) -> None:
    made: list[RollingOptionAdapter] = []

    def _factory() -> RollingOptionAdapter:
        adapter = _adapter(_PAYLOAD)
        made.append(adapter)
        return adapter

    request = DhanPullRequest(
        lake_root=tmp_path, start=date(2024, 6, 1), end=date(2024, 6, 8), dry_run=False
    )
    report = default_dhan_rolling_option_pull(request, adapter_factory=_factory, band=0)
    assert isinstance(report, DhanRollingOptionPullReport)
    assert len(made) == 1  # the seam built the adapter exactly once
    assert report.keys_planned == len(enumerate_rolling_option_keys(band=0))
    assert report.total_bars == report.keys_planned
    assert report.dry_run is False


def test_default_pull_seam_paces(tmp_path: Path) -> None:
    ticks: list[int] = []
    request = DhanPullRequest(
        lake_root=tmp_path, start=date(2024, 6, 1), end=date(2024, 6, 8), dry_run=False
    )
    report = default_dhan_rolling_option_pull(
        request,
        pace=lambda: ticks.append(1),
        adapter_factory=lambda: _adapter(_PAYLOAD),
        band=0,
    )
    assert ticks, "the injected pace hook was threaded into the rolling spine"
    assert report.dry_run is False


def test_default_pull_dry_run_builds_no_adapter_and_calls_no_vendor(tmp_path: Path) -> None:
    """HONEST dry-run: no adapter build, no vendor call, no write — Golden Rule #1."""
    built: list[RollingOptionAdapter] = []
    client = _FakeClient(_PAYLOAD)
    ticks: list[int] = []

    def _spy_factory() -> RollingOptionAdapter:
        adapter = RollingOptionAdapter(client=client)
        built.append(adapter)
        return adapter

    request = DhanPullRequest(
        lake_root=tmp_path, start=date(2024, 6, 1), end=date(2024, 6, 8), dry_run=True
    )
    report = default_dhan_rolling_option_pull(
        request,
        pace=lambda: ticks.append(1),
        adapter_factory=_spy_factory,
        band=0,
    )
    # The adapter_factory was NEVER invoked, and NO vendor call was issued.
    assert built == []
    assert client.calls == 0
    assert ticks == []  # the spine never ran, so pace never fired
    # An HONEST report: dry_run true, keys planned, zero bars, zero failures.
    assert report.dry_run is True
    assert report.keys_planned == len(enumerate_rolling_option_keys(band=0))
    assert report.total_bars == 0
    assert report.failed_windows == 0
    assert report.ingest.outcomes == ()
    # Nothing written to the lake.
    assert not (tmp_path / "feeds").exists()
    # The route payload is honest too.
    payload = report.to_dict()
    assert payload["dry_run"] is True
    assert payload["total_bars"] == 0
    assert payload["keys_planned"] == report.keys_planned


# ── report to_dict + HONESTY (rolling-ATM, never per-contract) ─────────────────


def test_report_to_dict_labels_rolling_atm(tmp_path: Path) -> None:
    request = DhanPullRequest(
        lake_root=tmp_path, start=date(2024, 6, 1), end=date(2024, 6, 8), dry_run=False
    )
    payload = default_dhan_rolling_option_pull(
        request, adapter_factory=lambda: _adapter(_PAYLOAD), band=0
    ).to_dict()
    assert payload["feed"] == "dhan"
    assert payload["dataset"] == "rolling-ATM"
    assert payload["market"] == "dhan-option"
    assert isinstance(payload["keys_planned"], int)
    # HONESTY LAW: never a per-contract / per-expiry / per-strike label anywhere.
    blob = repr(payload).lower()
    assert "per-contract" not in blob
    assert "per-expiry" not in blob
    assert "per-strike" not in blob


def test_module_source_has_no_per_contract_label() -> None:
    # Structural honesty pin: every "per-contract"/"per-expiry"/"per-strike" mention in
    # the module is a NEGATED disclaimer (NEVER / NOT / no ...) — never a positive label.
    # The rolling-pull module's own source, via the already-imported symbol's
    # defining file (avoids a redundant plain `import` of the module `from`-imported
    # at the top — CodeQL py/import-and-import-from).
    module_src = Path(run_dhan_rolling_option_pull.__code__.co_filename)
    _assert_per_contract_only_negated(module_src.read_text(encoding="utf-8"))


def _assert_per_contract_only_negated(text: str) -> None:
    """Assert every per-contract/expiry/strike mention has a nearby negator (never/not/no)."""
    src = " ".join(text.split()).lower()
    negators = ("never", "not ", "no ")
    for banned in ("per-contract", "per-expiry", "per-strike"):
        start = 0
        while (idx := src.find(banned, start)) != -1:
            window = src[max(0, idx - 60) : idx]
            assert any(n in window for n in negators), (
                f"{banned!r} used without a nearby negator (never/not/no): "
                f"...{src[max(0, idx - 60) : idx + len(banned)]}..."
            )
            start = idx + len(banned)


def test_universe_template_rolling_button_is_honest() -> None:
    # Local (no fastapi) SURFACE honesty audit: the universe.html rolling button is
    # present, posts the rolling route, is labelled "rolling-ATM", and — within the
    # rolling-specific HTML + JS regions — every "per-contract" mention is a NEGATED
    # disclaimer (the wider template legitimately says "per-contract default window"
    # about Groww, so the audit is scoped to the rolling regions this PR added).
    import brutex.dashboard as dash

    html = (Path(dash.__file__).parent / "templates" / "universe.html").read_text(encoding="utf-8")
    assert 'id="pullDhanOptBtn"' in html
    assert "/api/v1/universe/pull-rolling-options-dhan" in html
    assert "rolling-ATM" in html
    assert "NOT per-contract" in html
    # Scope to the two rolling regions this PR added (the HTML comment/button block and
    # the JS onclick block), each keyed off its "DHAN ROLLING-ATM options" marker.
    regions = 0
    for marker in ("DHAN ROLLING-ATM options feed", "DHAN ROLLING-ATM options pull"):
        idx = html.find(marker)
        assert idx != -1, f"missing rolling region marker {marker!r}"
        regions += 1
        _assert_per_contract_only_negated(html[idx : idx + 2000])
    assert regions == 2
