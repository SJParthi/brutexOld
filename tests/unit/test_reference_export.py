# pyright: reportPrivateUsage=none
"""Unit + mocked-S3 integration tests for the reference export (DEC-REFERENCE-EXPORT-S3-001).

Drives the REAL :mod:`brutex.export.reference_export` end-to-end against a real
on-disk lake (Parquet written the way the sink writes it) and an in-memory
``FakeS3Uploader`` (the ``FakeAwsClient`` pattern — NO boto3, NO network, NO
creds), and pins:

- the env resolvers (bucket fail-loud / enabled fail-loud);
- the wheel-free spot-partition discovery (every skip branch);
- the cleaned-day read (no-month / filtered-empty / rows);
- one full mocked export run — per-file Parquet uploaded, sha256 + row counts
  recorded, and the ``manifest.json`` written LAST describing a COMPLETE export;
- the FENCED seam (success returns the report; an uploader failure emits ONE
  WARN + a best-effort alert, writes NO manifest, and returns ``None``);
- the ``SweepAlerter.reference_export_failed`` method + the module emit helper.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

pytest.importorskip("polars")
import polars as pl
from brutex.export import reference_export as rx

from tests.fakes.fake_s3_uploader import FakeS3Uploader

if TYPE_CHECKING:
    pass

from structlog.testing import capture_logs

_DAY = date(2026, 7, 7)


def _write_month(
    lake: Path,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    on_day: date = _DAY,
    closes: tuple[float, ...] = (100.25, 100.50, 100.75),
    open_interest: int | None = None,
) -> None:
    """Write ``bars/<exch>/<seg>/<symbol>/1minute/<yyyy>/<mm>.parquet`` with real bars.

    Bars are placed at IST 09:15, 09:16, … on *on_day* (stored tz-aware UTC), the
    exact shape the sink writes (``timestamp`` + OHLCV + ``open_interest``).
    """
    base = lake / "bars" / exchange / segment / symbol / "1minute" / f"{on_day.year:04d}"
    base.mkdir(parents=True, exist_ok=True)
    # IST 09:15 == 03:45 UTC (fixed +05:30).
    timestamps = [
        datetime(on_day.year, on_day.month, on_day.day, 3, 45 + i, tzinfo=UTC)
        for i in range(len(closes))
    ]
    frame = pl.DataFrame(
        {
            "timestamp": timestamps,
            "open": [round(c - 0.25, 2) for c in closes],
            "high": [round(c + 0.10, 2) for c in closes],
            "low": [round(c - 0.30, 2) for c in closes],
            "close": list(closes),
            "volume": [10 * (i + 1) for i in range(len(closes))],
            "open_interest": [open_interest] * len(closes),
        },
        schema_overrides={"open_interest": pl.Int64},
    )
    frame.write_parquet(base / f"{on_day.month:02d}.parquet")


# ── resolvers ───────────────────────────────────────────────────────────


def test_resolve_export_bucket_reads_injected_mapping() -> None:
    """A non-blank injected env value is returned verbatim (stripped)."""
    assert (
        rx.resolve_export_bucket({rx.REFERENCE_EXPORT_BUCKET_ENV: "  my-bucket  "}) == "my-bucket"
    )


def test_resolve_export_bucket_blank_raises() -> None:
    """An unset/blank bucket is a loud config error — never a guessed name."""
    with pytest.raises(rx.ReferenceExportConfigError, match="never guesses"):
        rx.resolve_export_bucket({rx.REFERENCE_EXPORT_BUCKET_ENV: "   "})
    with pytest.raises(rx.ReferenceExportConfigError):
        rx.resolve_export_bucket({})


def test_resolve_export_bucket_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """``env=None`` reads ``os.environ``."""
    monkeypatch.setenv(rx.REFERENCE_EXPORT_BUCKET_ENV, "env-bucket")
    assert rx.resolve_export_bucket() == "env-bucket"


@pytest.mark.parametrize("token", ["1", "on", "true", "YES", " On "])
def test_resolve_enabled_on_tokens(token: str) -> None:
    assert rx.resolve_reference_export_enabled({rx.REFERENCE_EXPORT_ENABLED_ENV: token}) is True


@pytest.mark.parametrize("token", ["", "0", "off", "false", "NO"])
def test_resolve_enabled_off_tokens(token: str) -> None:
    assert rx.resolve_reference_export_enabled({rx.REFERENCE_EXPORT_ENABLED_ENV: token}) is False


def test_resolve_enabled_unset_defaults_off() -> None:
    assert rx.resolve_reference_export_enabled({}) is False


def test_resolve_enabled_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rx.REFERENCE_EXPORT_ENABLED_ENV, "1")
    assert rx.resolve_reference_export_enabled() is True


def test_resolve_enabled_junk_raises() -> None:
    with pytest.raises(ValueError, match="not a recognised switch"):
        rx.resolve_reference_export_enabled({rx.REFERENCE_EXPORT_ENABLED_ENV: "maybe"})


# ── discovery ───────────────────────────────────────────────────────────


def test_discover_missing_bars_dir_returns_empty(tmp_path: Path) -> None:
    assert rx.discover_reference_instruments(tmp_path) == []


def test_discover_skips_invalid_exchange_and_out_of_scope_segment(tmp_path: Path) -> None:
    """MCX (invalid exchange) + FNO (valid segment, out of scope) are excluded."""
    _write_month(tmp_path, exchange="NSE", segment="INDEX", symbol="NSE-NIFTY")
    # Invalid exchange dir.
    _write_month(tmp_path, exchange="MCX", segment="INDEX", symbol="MCX-GOLD")
    # Out-of-scope segment (FNO).
    _write_month(tmp_path, exchange="NSE", segment="FNO", symbol="NSE-NIFTY-CE")
    series = rx.discover_reference_instruments(tmp_path)
    assert [s.symbol for s in series] == ["NSE-NIFTY"]
    assert series[0].exchange == "NSE"
    assert series[0].segment == "INDEX"


def test_discover_skips_missing_and_empty_1minute_dir(tmp_path: Path) -> None:
    """A symbol with no ``1minute`` dir, or an empty one, is not discovered."""
    # Symbol with a non-1minute interval only.
    other = tmp_path / "bars" / "NSE" / "CASH" / "NSE-ONLY5M" / "5minute" / "2026"
    other.mkdir(parents=True)
    (other / "07.parquet").touch()
    # Symbol with an EMPTY 1minute dir (no parquet).
    empty = tmp_path / "bars" / "NSE" / "CASH" / "NSE-NOPARQUET" / "1minute"
    empty.mkdir(parents=True)
    # A real one.
    _write_month(tmp_path, exchange="NSE", segment="CASH", symbol="NSE-RELIANCE")
    assert [s.symbol for s in rx.discover_reference_instruments(tmp_path)] == ["NSE-RELIANCE"]


def test_discover_custom_segments_arg(tmp_path: Path) -> None:
    """The *segments* arg narrows the scope (INDEX-only here)."""
    _write_month(tmp_path, exchange="NSE", segment="INDEX", symbol="NSE-NIFTY")
    _write_month(tmp_path, exchange="NSE", segment="CASH", symbol="NSE-RELIANCE")
    got = rx.discover_reference_instruments(tmp_path, segments=("INDEX",))
    assert [s.symbol for s in got] == ["NSE-NIFTY"]


# ── cleaned-day read ────────────────────────────────────────────────────


def test_read_clean_day_frame_no_month_returns_none(tmp_path: Path) -> None:
    series = rx._SpotSeries(exchange="NSE", segment="INDEX", symbol="NSE-NIFTY")
    assert rx._read_clean_day_frame(tmp_path, series, _DAY) is None


def test_read_clean_day_frame_other_day_filters_to_none(tmp_path: Path) -> None:
    """A month file with bars only on a DIFFERENT day filters to empty → ``None``."""
    _write_month(
        tmp_path, exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", on_day=date(2026, 7, 8)
    )
    series = rx._SpotSeries(exchange="NSE", segment="INDEX", symbol="NSE-NIFTY")
    assert rx._read_clean_day_frame(tmp_path, series, _DAY) is None


def test_read_clean_day_frame_returns_projected_sorted_rows(tmp_path: Path) -> None:
    _write_month(tmp_path, exchange="NSE", segment="INDEX", symbol="NSE-NIFTY")
    series = rx._SpotSeries(exchange="NSE", segment="INDEX", symbol="NSE-NIFTY")
    frame = rx._read_clean_day_frame(tmp_path, series, _DAY)
    assert frame is not None
    assert frame.columns == list(rx.REFERENCE_EXPORT_COLUMNS)
    assert frame.height == 3
    assert frame.get_column("timestamp").is_sorted()


# ── full mocked export ──────────────────────────────────────────────────


def _seed_two_instruments_plus_a_noop(tmp_path: Path) -> None:
    _write_month(tmp_path, exchange="NSE", segment="INDEX", symbol="NSE-NIFTY")
    _write_month(tmp_path, exchange="NSE", segment="CASH", symbol="NSE-RELIANCE", open_interest=42)
    # Discovered, but has NO bars for _DAY → skipped (no upload).
    _write_month(
        tmp_path, exchange="BSE", segment="CASH", symbol="BSE-STALE", on_day=date(2026, 7, 8)
    )


def test_export_reference_day_writes_files_then_manifest_last(tmp_path: Path) -> None:
    """One full export: per-file Parquet + a manifest LAST that a consumer can trust."""
    _seed_two_instruments_plus_a_noop(tmp_path)
    uploader = FakeS3Uploader()

    report = rx.export_reference_day(tmp_path, _DAY, uploader=uploader, bucket="tv-archive")

    # Two instruments exported, one discovered-but-no-data skipped.
    assert report.instrument_count == 2
    assert report.bucket == "tv-archive"
    assert report.day_prefix == "groww-reference/2026-07-07"
    assert set(report.skipped_symbols) == {"BSE-STALE"}
    exported_keys = {f.key for f in report.files}
    assert exported_keys == {
        "groww-reference/2026-07-07/NSE-NIFTY.parquet",
        "groww-reference/2026-07-07/NSE-RELIANCE.parquet",
    }

    # Manifest LAST — every instrument object landed before it.
    assert uploader.put_keys[-1] == report.manifest_key
    assert report.manifest_key == "groww-reference/2026-07-07/manifest.json"
    assert all(k.endswith(".parquet") for k in uploader.put_keys[:-1])
    assert uploader.content_types[report.manifest_key] == "application/json"

    # Per-file sha256 + rows recorded honestly; the bytes round-trip to a frame.
    for entry in report.files:
        body = uploader.objects[entry.key]
        assert hashlib.sha256(body).hexdigest() == entry.sha256
        rt = pl.read_parquet(body)
        assert rt.height == entry.rows == 3
        assert rt.columns == list(rx.REFERENCE_EXPORT_COLUMNS)

    # Manifest shape: counts + per-file records the consumer keys on.
    manifest = json.loads(uploader.objects[report.manifest_key])
    assert manifest["source"] == rx.REFERENCE_EXPORT_SOURCE_TAG
    assert manifest["export_date"] == "2026-07-07"
    assert manifest["interval"] == "1minute"
    assert manifest["bucket"] == "tv-archive"
    assert manifest["prefix"] == "groww-reference/2026-07-07"
    assert manifest["instrument_count"] == 2
    assert manifest["skipped_count"] == 1
    assert manifest["skipped_symbols"] == ["BSE-STALE"]
    by_key = {f["key"]: f for f in manifest["files"]}
    assert by_key["groww-reference/2026-07-07/NSE-RELIANCE.parquet"]["segment"] == "CASH"
    assert by_key["groww-reference/2026-07-07/NSE-RELIANCE.parquet"]["rows"] == 3
    for entry in report.files:
        assert by_key[entry.key]["sha256"] == entry.sha256


def test_export_reference_day_empty_lake_writes_only_manifest(tmp_path: Path) -> None:
    """An empty lake still writes a manifest (instrument_count 0) — never a crash."""
    uploader = FakeS3Uploader()
    report = rx.export_reference_day(tmp_path, _DAY, uploader=uploader, bucket="b", log=None)
    assert report.instrument_count == 0
    assert uploader.put_keys == [report.manifest_key]
    manifest = json.loads(uploader.objects[report.manifest_key])
    assert manifest["instrument_count"] == 0
    assert manifest["files"] == []


def test_export_reference_day_custom_prefix_and_logger(tmp_path: Path) -> None:
    """A custom prefix + an explicit bound logger both thread through."""
    import structlog

    _write_month(tmp_path, exchange="NSE", segment="INDEX", symbol="NSE-NIFTY")
    uploader = FakeS3Uploader()
    report = rx.export_reference_day(
        tmp_path,
        _DAY,
        uploader=uploader,
        bucket="b",
        prefix="custom-ref",
        log=structlog.get_logger("t"),
    )
    assert report.day_prefix == "custom-ref/2026-07-07"
    assert report.files[0].key == "custom-ref/2026-07-07/NSE-NIFTY.parquet"


# ── fenced seam ─────────────────────────────────────────────────────────


def test_seam_success_returns_report(tmp_path: Path) -> None:
    _write_month(tmp_path, exchange="NSE", segment="INDEX", symbol="NSE-NIFTY")
    uploader = FakeS3Uploader()
    report = rx.run_reference_export_seam(tmp_path, _DAY, uploader=uploader, bucket="b")
    assert report is not None
    assert report.instrument_count == 1
    assert uploader.put_keys[-1] == report.manifest_key


def test_seam_failure_warns_alerts_writes_no_manifest_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An uploader failure is FENCED: WARN + alert, NO manifest, ``None`` return."""
    _write_month(tmp_path, exchange="NSE", segment="INDEX", symbol="NSE-NIFTY")
    # Fail the very first parquet put → the manifest is never reached.
    uploader = FakeS3Uploader(fail_on_key_substring=".parquet")

    alerts: list[dict[str, Any]] = []
    from brutex.notifications import sweep_alerts

    def _spy_alert(*, export_date: date, reason: str) -> None:
        alerts.append({"export_date": export_date, "reason": reason})

    monkeypatch.setattr(sweep_alerts, "emit_reference_export_failed_alert", _spy_alert)

    with capture_logs() as logs:
        report = rx.run_reference_export_seam(tmp_path, _DAY, uploader=uploader, bucket="b")

    assert report is None
    assert not any(k.endswith("manifest.json") for k in uploader.put_keys)  # never written
    warns = [e for e in logs if e["event"] == "reference_export.failed"]
    assert len(warns) == 1
    assert warns[0]["error_type"] == "FakeS3Error"
    assert len(alerts) == 1
    assert alerts[0]["export_date"] == _DAY
    assert "FakeS3Error" in alerts[0]["reason"]


def test_seam_failure_alert_disabled_still_warns_and_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``alert=False`` skips the Telegram alert but still WARNs + returns ``None``."""
    _write_month(tmp_path, exchange="NSE", segment="INDEX", symbol="NSE-NIFTY")
    uploader = FakeS3Uploader(fail_on_key_substring=".parquet")
    from brutex.notifications import sweep_alerts

    def _boom(**_kwargs: Any) -> None:
        raise AssertionError("alert must not fire when alert=False")

    monkeypatch.setattr(sweep_alerts, "emit_reference_export_failed_alert", _boom)

    with capture_logs() as logs:
        report = rx.run_reference_export_seam(
            tmp_path, _DAY, uploader=uploader, bucket="b", alert=False
        )
    assert report is None
    assert any(e["event"] == "reference_export.failed" for e in logs)


def test_seam_manifest_failure_also_fenced(tmp_path: Path) -> None:
    """A failure on the MANIFEST put alone is fenced too (files landed, no manifest)."""
    _write_month(tmp_path, exchange="NSE", segment="INDEX", symbol="NSE-NIFTY")
    uploader = FakeS3Uploader(fail_on_key_substring="manifest.json")
    report = rx.run_reference_export_seam(
        tmp_path, _DAY, uploader=uploader, bucket="b", alert=False
    )
    assert report is None
    # The instrument file DID land, but no manifest → consumer detects incomplete.
    assert any(k.endswith("NSE-NIFTY.parquet") for k in uploader.put_keys)
    assert not any(k.endswith("manifest.json") for k in uploader.put_keys)


# ── SweepAlerter method + emit helper ───────────────────────────────────


class _FakeNotifier:
    """Records published events (the SweepAlerter publish surface)."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def publish(self, event: Any) -> None:
        self.events.append(event)


def test_reference_export_failed_publishes_warn_event() -> None:
    from brutex.notifications.sweep_alerts import SweepAlerter
    from brutex.notifications.telegram import TelegramEventKind, TelegramSeverity

    notifier = _FakeNotifier()
    ok = SweepAlerter(notifier=notifier).reference_export_failed(
        export_date="2026-07-07", reason="FakeS3Error: denied"
    )
    assert ok is True
    (event,) = notifier.events
    assert event.kind == TelegramEventKind.PHASE_BLOCKED
    assert event.severity == TelegramSeverity.WARN
    assert event.dedupe_key == "reference-export:2026-07-07"
    # MarkdownV2-escaped body (hyphens escaped) — the dedupe_key carries the raw date.
    assert "2026\\-07\\-07" in event.message
    assert "Reference export FAILED" in event.message


def test_reference_export_failed_no_notifier_returns_false() -> None:
    from brutex.notifications.sweep_alerts import SweepAlerter

    ok = SweepAlerter(resolver=lambda: None).reference_export_failed(
        export_date="2026-07-07", reason="x"
    )
    assert ok is False


def test_emit_helper_silent_when_disabled() -> None:
    """The module helper is a silent no-op when no notifier resolves (default)."""
    from brutex.notifications.sweep_alerts import emit_reference_export_failed_alert

    # Must not raise (Telegram disabled → resolver yields None → _publish False).
    emit_reference_export_failed_alert(export_date=_DAY, reason="boom")


def test_emit_helper_swallows_alerter_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising alerter is swallowed + logged — the fire-and-forget contract."""
    from brutex.notifications import sweep_alerts

    class _Boom:
        def reference_export_failed(self, **_kwargs: Any) -> bool:
            raise RuntimeError("telegram down")

    monkeypatch.setattr(sweep_alerts, "SweepAlerter", _Boom)
    # Must NOT raise.
    sweep_alerts.emit_reference_export_failed_alert(export_date=_DAY, reason="boom")


# ── build_real_s3_uploader + resolve_export_region (round-4 refutation fixes) ──
#
# The client build is the LAST unfenced link: `boto3.client(...)` raises
# non-boto3-typed errors on config gaps (botocore InvalidRegionError is a
# ValueError subclass; ProfileNotFound/DataNotFoundError are their own trees) —
# none were ReferenceExportConfigError, so they escaped both CLI shims' guards.
# These tests drive the REAL build (pragma lifted) via a fake `boto3` module in
# sys.modules — boto3 itself is NOT installed in the test env, by design.


def _install_fake_boto3(
    monkeypatch: pytest.MonkeyPatch, client_factory: Any
) -> list[dict[str, Any]]:
    """Install a stand-in ``boto3`` module whose ``client`` records its kwargs."""
    import sys
    import types

    calls: list[dict[str, Any]] = []

    def _client(service: str, **kwargs: Any) -> Any:
        calls.append({"service": service, **kwargs})
        return client_factory(service, **kwargs)

    fake = types.ModuleType("boto3")
    fake.client = _client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake)
    return calls


def test_resolve_export_region_strips_value() -> None:
    assert rx.resolve_export_region({rx.REFERENCE_EXPORT_REGION_ENV: " us-east-1 "}) == "us-east-1"


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_resolve_export_region_blank_after_strip_is_none(value: str) -> None:
    """Whitespace-only region env resolves None (falls back to the default region)."""
    assert rx.resolve_export_region({rx.REFERENCE_EXPORT_REGION_ENV: value}) is None


def test_resolve_export_region_unset_is_none() -> None:
    assert rx.resolve_export_region({}) is None


def test_resolve_export_region_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rx.REFERENCE_EXPORT_REGION_ENV, " ap-south-1 ")
    assert rx.resolve_export_region() == "ap-south-1"


def test_build_real_s3_uploader_boto3_missing_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing boto3 raises ReferenceExportConfigError naming the extra."""
    import sys

    monkeypatch.setitem(sys.modules, "boto3", None)  # import boto3 → ImportError
    with pytest.raises(rx.ReferenceExportConfigError, match="boto3 is required"):
        rx.build_real_s3_uploader(bucket="b")


def test_build_real_s3_uploader_converts_invalid_region_valueerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """botocore InvalidRegionError (a ValueError subclass) converts to the fence type."""

    class InvalidRegionError(ValueError):
        """Shape of botocore.exceptions.InvalidRegionError."""

    def _raise(service: str, **kwargs: Any) -> Any:
        raise InvalidRegionError(
            "Provided region_name 'not a region!' doesn't match a supported format."
        )

    _install_fake_boto3(monkeypatch, _raise)
    with pytest.raises(rx.ReferenceExportConfigError, match="InvalidRegionError") as excinfo:
        rx.build_real_s3_uploader(bucket="b", region="not a region!")
    assert isinstance(excinfo.value.__cause__, InvalidRegionError)  # chained, never dropped
    assert "not a region!" in str(excinfo.value)  # names the offending region


def test_build_real_s3_uploader_converts_generic_botocore_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ProfileNotFound-shaped (non-ValueError) build failure also converts."""

    class ProfileNotFound(Exception):
        """Shape of botocore.exceptions.ProfileNotFound."""

    def _raise(service: str, **kwargs: Any) -> Any:
        raise ProfileNotFound("The config profile (nope) could not be found")

    _install_fake_boto3(monkeypatch, _raise)
    with pytest.raises(rx.ReferenceExportConfigError, match="ProfileNotFound"):
        rx.build_real_s3_uploader(bucket="b")


def test_build_real_s3_uploader_success_binds_default_region_and_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Success path: default region bound, put_object delegates WRITE-ONLY kwargs."""
    puts: list[dict[str, Any]] = []

    class _FakeClient:
        def put_object(self, **kwargs: Any) -> None:
            puts.append(kwargs)

    calls = _install_fake_boto3(monkeypatch, lambda service, **kwargs: _FakeClient())
    uploader = rx.build_real_s3_uploader(bucket="tickvault-ref")

    assert calls == [{"service": "s3", "region_name": rx.DEFAULT_REFERENCE_EXPORT_REGION}]
    uploader.put_object(
        key="k/file.parquet", body=b"bytes", content_type="application/octet-stream"
    )
    assert puts == [
        {
            "Bucket": "tickvault-ref",
            "Key": "k/file.parquet",
            "Body": b"bytes",
            "ContentType": "application/octet-stream",
        }
    ]


def test_build_real_s3_uploader_explicit_region_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_boto3(monkeypatch, lambda service, **kwargs: object())
    rx.build_real_s3_uploader(bucket="b", region="us-east-1")
    assert calls == [{"service": "s3", "region_name": "us-east-1"}]
