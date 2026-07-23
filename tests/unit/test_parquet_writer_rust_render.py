"""Wiring tests for the ``_render_bytes`` Rust-render seam (ADR-0020 PR-1).

DEC-RUST-DATALAKE-IO-ROADMAP-001 (MIGRATION.md #1796). Crate-FREE: a fake
``brutex_lake`` module (implemented ON TOP of the installed Python polars, so
its output is genuinely byte-comparable) drives the flag-ON routing through
the REAL :meth:`ParquetWriter._render_bytes`; the flag-OFF arm is pinned
byte-identical with a never-routes spy. Real-crate byte parity lives in
``tests/golden/test_lake_rust_render_parity.py``.

Whitebox (DEC-P0-017): exercises ``_render_bytes``/``_ipc_bytes`` directly.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import io
import sys
import types
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from brutex.data import lake_rust_render as lrr
from brutex.data.errors import LakeWriteHalt
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ROW_GROUP_SIZE, ZSTD_LEVEL, ParquetWriter, _ipc_bytes
from structlog.testing import capture_logs


@pytest.fixture(autouse=True)
def _fresh_process_view() -> Iterator[None]:
    lrr.reset_lake_rust_render_for_tests()
    yield
    lrr.reset_lake_rust_render_for_tests()


def _bars(n: int = 4) -> list[CandleBar]:
    return [
        CandleBar(
            timestamp=datetime(2024, 1, 1, 3, 45 + i, tzinfo=UTC),
            open=Decimal("100.25"),
            high=Decimal("101.00"),
            low=Decimal("99.75"),
            close=Decimal("100.50"),
            volume=1_000 + i,
            open_interest=None if i % 2 else 10 + i,
        )
        for i in range(n)
    ]


def _polars_backed_fake_crate() -> types.ModuleType:
    """A fake crate whose render IS the installed wheel's render.

    Reads the IPC handoff back into a DataFrame and renders it with the
    verbatim writer options — byte-identical to ``_python_render`` by
    construction, so the burn-in compare passes and the ROUTED path is
    proven end-to-end without the compiled crate.
    """
    import polars as pl

    module = types.ModuleType("brutex_lake")

    def render_parquet_from_ipc(ipc_bytes: bytes) -> bytes:
        df = pl.read_ipc(io.BytesIO(ipc_bytes))
        bio = io.BytesIO()
        df.write_parquet(
            bio,
            compression="zstd",
            compression_level=ZSTD_LEVEL,
            row_group_size=ROW_GROUP_SIZE,
            use_pyarrow=False,
            statistics=False,
        )
        return bio.getvalue()

    def render_parquet_defaults_from_ipc(ipc_bytes: bytes) -> bytes:
        # Present only to satisfy the all-or-nothing _CRATE_API surface
        # check (PR-2) — this suite drives the BARS profile exclusively.
        df = pl.read_ipc(io.BytesIO(ipc_bytes))
        bio = io.BytesIO()
        df.write_parquet(bio)
        return bio.getvalue()

    module.render_parquet_from_ipc = render_parquet_from_ipc  # type: ignore[attr-defined]
    module.render_parquet_defaults_from_ipc = (  # type: ignore[attr-defined]
        render_parquet_defaults_from_ipc
    )
    module.pinned_polars_version = lambda: pl.__version__  # type: ignore[attr-defined]
    module.linked_zstd_version = lambda: "fake"  # type: ignore[attr-defined]
    return module


class TestFlagOff:
    def test_kill_switch_never_touches_the_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # OFF is addressed via the explicit "0" kill switch since the
        # default flipped ON (DEC-RUST-DEFAULT-ON-WAVE-001) — the never-routes teeth survive.
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "0")

        def _must_not_route(**_: object) -> bytes:
            raise AssertionError("flag-OFF must never call render_parquet_bytes")

        monkeypatch.setattr(lrr, "render_parquet_bytes", _must_not_route)
        rendered = ParquetWriter._render_bytes(_bars())
        assert rendered[:4] == b"PAR1"

    def test_off_and_on_with_faithful_crate_are_byte_identical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _bars()
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "0")  # the kill switch
        off_bytes = ParquetWriter._render_bytes(bars)

        monkeypatch.setitem(sys.modules, "brutex_lake", _polars_backed_fake_crate())
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "1")
        lrr.reset_lake_rust_render_for_tests()
        with capture_logs() as logs:
            on_bytes = ParquetWriter._render_bytes(bars)
        assert on_bytes == off_bytes
        assert not lrr.rust_render_disabled()  # burn-in compare passed
        assert any(e["event"] == "lake_rust_render.engaged" for e in logs)


class TestFlagOnGuards:
    def test_absent_crate_serves_identical_python_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _bars()
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "0")  # the kill switch → Python oracle
        expected = ParquetWriter._render_bytes(bars)

        monkeypatch.setitem(sys.modules, "brutex_lake", None)  # → ImportError
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "1")
        lrr.reset_lake_rust_render_for_tests()
        with capture_logs() as logs:
            rendered = ParquetWriter._render_bytes(bars)
        assert rendered == expected
        assert lrr.rust_render_disabled()
        disabled = [e for e in logs if e["event"] == "lake_rust_render.rust_path_disabled"]
        assert disabled and disabled[0]["reason"] == "crate_unavailable"

    def test_version_mismatch_serves_identical_python_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _bars()
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "0")  # the kill switch → Python oracle
        expected = ParquetWriter._render_bytes(bars)

        fake = _polars_backed_fake_crate()
        fake.pinned_polars_version = lambda: "0.0.0-mismatch"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "brutex_lake", fake)
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "1")
        lrr.reset_lake_rust_render_for_tests()
        with capture_logs() as logs:
            rendered = ParquetWriter._render_bytes(bars)
        assert rendered == expected
        disabled = [e for e in logs if e["event"] == "lake_rust_render.rust_path_disabled"]
        assert disabled and disabled[0]["reason"] == "polars_version_mismatch"

    def test_crate_memory_error_maps_to_lake_write_halt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OOM on the engaged path rides ``lake_write_guard`` → ``LakeWriteHalt``.

        Identical to the pure-Python render's OOM contract
        (DEC-LAKE-WRITE-HALT-001) — the seam introduces no new crash class
        AND removes none.
        """
        import polars as pl

        fake = types.ModuleType("brutex_lake")
        fake.pinned_polars_version = lambda: pl.__version__  # type: ignore[attr-defined]
        fake.linked_zstd_version = lambda: "fake"  # type: ignore[attr-defined]

        def _oom(ipc_bytes: bytes) -> bytes:
            raise MemoryError

        fake.render_parquet_from_ipc = _oom  # type: ignore[attr-defined]
        fake.render_parquet_defaults_from_ipc = _oom  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "brutex_lake", fake)
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "1")
        lrr.reset_lake_rust_render_for_tests()
        with pytest.raises(LakeWriteHalt) as excinfo:
            ParquetWriter._render_bytes(_bars())
        assert excinfo.value.reason == "out_of_memory"


class TestSchemaTokenWiring:
    def test_seam_passes_the_frame_schema_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The production seam keys the burn-in SHAPE gate (review-round fold).

        A ``None`` token would silently degrade the shape gate to the count
        budget alone — the ONE production caller must pass the frame's
        schema fingerprint.
        """
        captured: dict[str, object] = {}

        def _capture(**kwargs: object) -> bytes:
            captured.update(kwargs)
            return b"routed"

        monkeypatch.setattr(lrr, "render_parquet_bytes", _capture)
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "1")
        assert ParquetWriter._render_bytes(_bars()) == b"routed"
        token = captured["schema_token"]
        assert isinstance(token, str) and token  # non-None, non-empty
        assert "timestamp" in token  # the frame schema fingerprint, not a constant

    def test_spot_and_greeks_frames_carry_distinct_tokens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.data.models import FnoGreeksBar

        tokens: list[object] = []

        def _capture(**kwargs: object) -> bytes:
            tokens.append(kwargs["schema_token"])
            return b"routed"

        monkeypatch.setattr(lrr, "render_parquet_bytes", _capture)
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "1")
        spot = _bars()
        greeks: list[CandleBar] = [
            FnoGreeksBar(
                timestamp=b.timestamp,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                open_interest=b.open_interest,
            )
            for b in spot
        ]
        ParquetWriter._render_bytes(spot)
        ParquetWriter._render_bytes(greeks)
        assert len(tokens) == 2
        assert tokens[0] != tokens[1]  # 7-col spot vs 17-col Greeks overlay


class TestIpcHandoff:
    def test_ipc_bytes_round_trips_the_frame(self) -> None:
        import polars as pl

        df = pl.DataFrame(
            {"timestamp": [datetime(2024, 1, 1, 3, 45, tzinfo=UTC)], "close": [100.5]},
            schema={
                "timestamp": pl.Datetime(time_unit="us", time_zone="UTC"),
                "close": pl.Float64,
            },
        )
        back = pl.read_ipc(io.BytesIO(_ipc_bytes(df)))
        assert back.equals(df)
