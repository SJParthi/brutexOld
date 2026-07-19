"""Unit suite for the default-OFF cross-verify Rust seam (DEC-CROSSVERIFY-RUST-CORE).

Covers :mod:`brutex.crossverify.rust_seam` at 100% line+branch STANDALONE
(the ci.yml per-module gate) plus the flag-gated wiring arms inside
:mod:`brutex.crossverify.csv_render` / :mod:`brutex.crossverify.canonical`:

* the flag matrix (truthy / falsy / unset / junk-warns-once) + the LOCAL
  ``_TRUTHY``/``_FALSY`` vocabulary equality-pinned against the engine's
  :mod:`brutex.engine.sweep_lookahead` (the #1891 local-twin precedent);
* flag-OFF NEVER touches the crate (bomb-crate spy — resolver AND
  ``sys.modules`` both bombed);
* wheel-absent (``crate_unavailable``) / stale-wheel (``crate_stale``)
  latches, warn-once semantics;
* the per-law 16-call burn-in bit-compare + ship-the-ORACLE autorevert
  (one ERROR) and the burn-in-complete countdown;
* the degrade set, the pyo3 panic belt, ``MemoryError`` raw,
  ``KeyboardInterrupt``/``SystemExit`` propagation;
* the refuse-poison gates (typed CSV refusal codes, malformed tuples,
  non-bytes/non-str returns).

Every fake crate is a plain ``types.ModuleType`` planted in ``sys.modules``
(``importlib.import_module`` serves the planted entry — no real wheel is
required; the REAL-wheel arms live in the ops-rust-parity lane's golden +
differential suites).
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
import structlog.testing
from brutex.calendars.nse import get_session_checked
from brutex.crossverify import canonical, csv_render, rust_seam
from brutex.crossverify.canonical import CanonicalIdentity, canonical_identity, s3_key
from brutex.crossverify.csv_render import CSV_HEADER, ExportContractError, ExportRow, render_day_csv

if TYPE_CHECKING:
    from collections.abc import Iterator

#: A verified regular NSE session (the golden harness day).
_DAY = date(2026, 7, 10)

_IDENTITY = CanonicalIdentity(
    exchange="NSE",
    lake_segment="CASH",
    lake_symbol="M&M",
    canonical_symbol="M&M",
    segment_token="NSE_EQ",
    instrument_class="cash_spot",
)


def _session() -> object:
    session = get_session_checked(_DAY)
    assert session is not None
    return session


def _rows(n: int = 2) -> list[ExportRow]:
    out: list[ExportRow] = []
    for minute in range(n):
        base = Decimal("2900.00") + Decimal(minute) * Decimal("0.05")
        out.append(
            ExportRow(
                ts_utc=datetime(2026, 7, 10, 3, 45 + minute, tzinfo=UTC),
                open=base,
                high=base + Decimal("0.30"),
                low=base - Decimal("0.25"),
                close=base + Decimal("0.10"),
                volume=100 + minute,
                oi=None,
            )
        )
    return out


def _oracle_bytes(n: int = 2) -> bytes:
    return csv_render._render_day_csv_python(identity=_IDENTITY, rows=_rows(n), session=_session())


def _plant_crate(monkeypatch: pytest.MonkeyPatch, **functions: object) -> types.ModuleType:
    """Plant a fake ``brutex_ops`` module carrying *functions* (full API by default)."""
    module = types.ModuleType("brutex_ops")
    module.__version__ = "0.0-test"
    defaults: dict[str, object] = {
        "crossverify_render_csv": lambda header, rows: (b"", None),
        "crossverify_canonical_identity": lambda *args: None,
        "crossverify_s3_key": lambda *args: "",
    }
    defaults.update(functions)
    for name, fn in defaults.items():
        setattr(module, name, fn)
    monkeypatch.setitem(sys.modules, "brutex_ops", module)
    return module


def _faithful_crate(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """A fake crate whose three laws agree with the Python oracles."""

    def render(header: str, rows: list[list[str]]) -> tuple[bytes | None, tuple | None]:
        if not rows:
            return None, (1, 0, 0)
        lines = [header] + [",".join(cells) for cells in rows]
        return ("\n".join(lines) + "\n").encode("utf-8"), None

    def identity(
        exchange: str,
        segment: str,
        lake_symbol: str,
        fno_underlying: str | None,
        fno_expiry_iso: str | None,
        fno_is_future: bool,
        fno_exchange: str | None,
    ) -> tuple[str, str, str, str, str, str] | None:
        return canonical._identity_fields(
            canonical._canonical_identity_python(
                exchange=exchange, segment=segment, lake_symbol=lake_symbol
            )
        )

    def key(prefix: str, day_iso: str, segment_token: str, symbol: str) -> str:
        return f"{prefix}/{day_iso}/{segment_token}/{symbol}.csv"

    return _plant_crate(
        monkeypatch,
        crossverify_render_csv=render,
        crossverify_canonical_identity=identity,
        crossverify_s3_key=key,
    )


@pytest.fixture(autouse=True)
def _fresh_seam() -> Iterator[None]:
    """Reset the seam latch/burn-in/memo around every test (isolation)."""
    rust_seam.reset_crossverify_rust_for_tests()
    yield
    rust_seam.reset_crossverify_rust_for_tests()


@pytest.fixture
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, "1")


class TestFlagVocabulary:
    """The flag matrix + the engine-vocabulary equality pin (#1891)."""

    def test_truthy_falsy_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for raw in ("1", "true", "YES", " on "):
            monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, raw)
            assert rust_seam.crossverify_rust_enabled() is True
        for raw in ("0", "false", "No", " OFF "):
            monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, raw)
            assert rust_seam.crossverify_rust_enabled() is False
        monkeypatch.delenv(rust_seam.CROSSVERIFY_RUST_ENV, raising=False)
        assert rust_seam.crossverify_rust_enabled() is False
        monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, "   ")
        assert rust_seam.crossverify_rust_enabled() is False

    def test_junk_flag_warns_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, "banana")
        with structlog.testing.capture_logs() as logs:
            assert rust_seam.crossverify_rust_enabled() is False
            assert rust_seam.crossverify_rust_enabled() is False
        warns = [e for e in logs if e["event"] == "crossverify_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"

    def test_vocabulary_equality_pinned_to_engine(self) -> None:
        """The LOCAL twin must never drift from the engine vocabulary."""
        from brutex.engine import sweep_lookahead

        assert rust_seam._TRUTHY == sweep_lookahead._TRUTHY
        assert rust_seam._FALSY == sweep_lookahead._FALSY
        for raw in ("1", "true", "YES", " on ", "0", "false", "No", " OFF ", "", "  ", "junk"):
            assert rust_seam.env_flag_tristate(raw) == sweep_lookahead.env_flag_tristate(raw)


class TestFlagOffNeverTouchesCrate:
    """Flag OFF is byte-identical AND crate-untouched (the bomb spy)."""

    def test_all_three_laws_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(rust_seam.CROSSVERIFY_RUST_ENV, raising=False)

        def bomb() -> None:  # pragma: no cover — detonation IS the failure
            raise AssertionError("flag-OFF touched the crate resolver")

        monkeypatch.setattr(rust_seam, "_resolve_crate", bomb)

        class _Bomb(types.ModuleType):
            def __getattr__(self, name: str) -> object:  # pragma: no cover
                raise AssertionError("flag-OFF touched brutex_ops")

        monkeypatch.setitem(sys.modules, "brutex_ops", _Bomb("brutex_ops"))
        assert render_day_csv(identity=_IDENTITY, rows=_rows(), session=_session()) == (
            _oracle_bytes()
        )
        identity = canonical_identity(exchange="NSE", segment="CASH", lake_symbol="M&M")
        assert identity == _IDENTITY
        assert (
            s3_key(prefix="crossverify/groww", day=_DAY, identity=_IDENTITY)
            == "crossverify/groww/2026-07-10/NSE_EQ/M&M.csv"
        )

    def test_explicit_falsy_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, "0")
        monkeypatch.setattr(
            rust_seam,
            "_resolve_crate",
            lambda: pytest.fail("explicit-OFF touched the crate"),
        )
        assert render_day_csv(identity=_IDENTITY, rows=_rows(), session=_session()) == (
            _oracle_bytes()
        )


class TestCrateGuards:
    """Import guard + API-surface handshake + latch semantics."""

    def test_wheel_absent_latches_crate_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        monkeypatch.delitem(sys.modules, "brutex_ops", raising=False)
        real_import = rust_seam.importlib.import_module

        def fake_import(name: str, *args: object) -> object:
            if name == "brutex_ops":
                raise ImportError("no wheel")
            return real_import(name, *args)  # pragma: no cover — no other import fires

        monkeypatch.setattr(rust_seam.importlib, "import_module", fake_import)
        with structlog.testing.capture_logs() as logs:
            out = render_day_csv(identity=_IDENTITY, rows=_rows(), session=_session())
        assert out == _oracle_bytes()
        assert rust_seam.rust_seam_disabled() is True
        disabled = [e for e in logs if e["event"] == "crossverify_rust.rust_path_disabled"]
        assert len(disabled) == 1
        assert disabled[0]["reason"] == "crate_unavailable"
        # The latch short-circuits the next call — no second import attempt.
        monkeypatch.setattr(
            rust_seam.importlib,
            "import_module",
            lambda *a: pytest.fail("latched seam re-imported"),
        )
        assert render_day_csv(identity=_IDENTITY, rows=_rows(), session=_session()) == (
            _oracle_bytes()
        )

    def test_stale_wheel_latches_crate_stale(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        module = types.ModuleType("brutex_ops")
        module.crossverify_render_csv = lambda header, rows: (b"", None)
        # canonical + s3_key MISSING — a pre-crossverify-lane wheel.
        monkeypatch.setitem(sys.modules, "brutex_ops", module)
        with structlog.testing.capture_logs() as logs:
            out = rust_seam.routed_s3_key(
                prefix="p",
                day_iso="2026-07-10",
                segment_token="NSE_EQ",
                canonical_symbol="M&M",
                python_oracle=lambda: "oracle-key",
            )
        assert out == "oracle-key"
        disabled = [e for e in logs if e["event"] == "crossverify_rust.rust_path_disabled"]
        assert disabled[0]["reason"] == "crate_stale"

    def test_non_callable_api_member_is_stale(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        module = _plant_crate(monkeypatch)
        module.crossverify_canonical_identity = "not callable"
        fields = rust_seam.routed_canonical_identity(
            exchange="NSE",
            segment="CASH",
            lake_symbol="M&M",
            fno_underlying=None,
            fno_expiry_iso=None,
            fno_is_future=False,
            fno_exchange=None,
            python_oracle=lambda: ("a", "b", "c", "d", "e", "f"),
        )
        assert fields == ("a", "b", "c", "d", "e", "f")
        assert rust_seam.rust_seam_disabled() is True

    def test_disable_repeat_is_noop(self) -> None:
        rust_seam._disable("first")
        with structlog.testing.capture_logs() as logs:
            rust_seam._disable("second", error=True)
        assert logs == []
        assert rust_seam._DISABLED_REASON[0] == "first"


class TestPanicBelt:
    """The guarded pyo3_runtime.PanicException resolution + the belt arm."""

    def test_import_error_gives_exception_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_import = rust_seam.importlib.import_module

        def fake_import(name: str, *args: object) -> object:
            if name == "pyo3_runtime":
                raise ImportError("absent")
            return real_import(name, *args)  # pragma: no cover — no other import fires

        monkeypatch.setattr(rust_seam.importlib, "import_module", fake_import)
        assert rust_seam._panic_belt_errors() == (Exception,)

    def test_panic_class_joins_belt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class PanicException(BaseException):
            pass

        module = types.ModuleType("pyo3_runtime")
        module.PanicException = PanicException
        monkeypatch.setitem(sys.modules, "pyo3_runtime", module)
        assert rust_seam._panic_belt_errors() == (Exception, PanicException)

    def test_non_type_or_exception_alias_excluded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = types.ModuleType("pyo3_runtime")
        module.PanicException = "not a type"
        monkeypatch.setitem(sys.modules, "pyo3_runtime", module)
        assert rust_seam._panic_belt_errors() == (Exception,)
        # An alias of Exception itself is already in the belt — not appended.
        module.PanicException = Exception
        assert rust_seam._panic_belt_errors() == (Exception,)
        # A non-BaseException type is refused by the subclass check.
        module.PanicException = int
        assert rust_seam._panic_belt_errors() == (Exception,)

    def test_panic_latches_crate_panic(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        class PanicException(BaseException):
            pass

        panic_module = types.ModuleType("pyo3_runtime")
        panic_module.PanicException = PanicException
        monkeypatch.setitem(sys.modules, "pyo3_runtime", panic_module)

        def boom(header: str, rows: object) -> tuple:
            raise PanicException("kernel panicked")

        _plant_crate(monkeypatch, crossverify_render_csv=boom)
        with structlog.testing.capture_logs() as logs:
            out = rust_seam.routed_render_csv(
                CSV_HEADER, [["a"] * 8], python_render=lambda: b"oracle"
            )
        assert out == b"oracle"
        disabled = [e for e in logs if e["event"] == "crossverify_rust.rust_path_disabled"]
        assert disabled[0]["reason"] == "crate_panic"

    def test_panic_latches_crate_panic_canonical_and_s3_key(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        """The panic belt + the post-latch short-circuit on the OTHER two laws."""

        class PanicException(BaseException):
            pass

        panic_module = types.ModuleType("pyo3_runtime")
        panic_module.PanicException = PanicException
        monkeypatch.setitem(sys.modules, "pyo3_runtime", panic_module)

        def boom(*args: object) -> None:
            raise PanicException("kernel panicked")

        _plant_crate(monkeypatch, crossverify_canonical_identity=boom)
        fields = rust_seam.routed_canonical_identity(
            exchange="NSE",
            segment="CASH",
            lake_symbol="M&M",
            fno_underlying=None,
            fno_expiry_iso=None,
            fno_is_future=False,
            fno_exchange=None,
            python_oracle=lambda: ("o",) * 6,
        )
        assert fields == ("o",) * 6
        assert rust_seam.rust_seam_disabled() is True
        # The latch short-circuits BOTH remaining laws (line-level teeth).
        assert (
            rust_seam.routed_s3_key(
                prefix="p",
                day_iso="2026-07-10",
                segment_token="NSE_EQ",
                canonical_symbol="M&M",
                python_oracle=lambda: "oracle-key",
            )
            == "oracle-key"
        )
        assert (
            rust_seam.routed_canonical_identity(
                exchange="NSE",
                segment="CASH",
                lake_symbol="M&M",
                fno_underlying=None,
                fno_expiry_iso=None,
                fno_is_future=False,
                fno_exchange=None,
                python_oracle=lambda: None,
            )
            is None
        )
        # And the s3_key panic belt itself (a fresh seam, s3_key crate bomb).
        rust_seam.reset_crossverify_rust_for_tests()
        _plant_crate(monkeypatch, crossverify_s3_key=boom)
        assert (
            rust_seam.routed_s3_key(
                prefix="p",
                day_iso="2026-07-10",
                segment_token="NSE_EQ",
                canonical_symbol="M&M",
                python_oracle=lambda: "oracle-key",
            )
            == "oracle-key"
        )
        assert rust_seam._DISABLED_REASON[0] == "crate_panic"


class TestEscapeSet:
    """MemoryError raw; KeyboardInterrupt/SystemExit propagate — all laws."""

    @pytest.mark.parametrize("exc_type", [MemoryError, KeyboardInterrupt, SystemExit])
    def test_csv_law_escapes(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None, exc_type: type[BaseException]
    ) -> None:
        def boom(header: str, rows: object) -> tuple:
            raise exc_type()

        _plant_crate(monkeypatch, crossverify_render_csv=boom)
        with pytest.raises(exc_type):
            rust_seam.routed_render_csv(CSV_HEADER, [["a"] * 8], python_render=lambda: b"o")
        assert rust_seam.rust_seam_disabled() is False

    @pytest.mark.parametrize("exc_type", [MemoryError, KeyboardInterrupt, SystemExit])
    def test_canonical_law_escapes(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None, exc_type: type[BaseException]
    ) -> None:
        def boom(*args: object) -> None:
            raise exc_type()

        _plant_crate(monkeypatch, crossverify_canonical_identity=boom)
        with pytest.raises(exc_type):
            rust_seam.routed_canonical_identity(
                exchange="NSE",
                segment="CASH",
                lake_symbol="M&M",
                fno_underlying=None,
                fno_expiry_iso=None,
                fno_is_future=False,
                fno_exchange=None,
                python_oracle=lambda: None,
            )

    @pytest.mark.parametrize("exc_type", [MemoryError, KeyboardInterrupt, SystemExit])
    def test_s3_key_law_escapes(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None, exc_type: type[BaseException]
    ) -> None:
        def boom(*args: object) -> None:
            raise exc_type()

        _plant_crate(monkeypatch, crossverify_s3_key=boom)
        with pytest.raises(exc_type):
            rust_seam.routed_s3_key(
                prefix="p",
                day_iso="2026-07-10",
                segment_token="NSE_EQ",
                canonical_symbol="M&M",
                python_oracle=lambda: "k",
            )


class TestDegradeSet:
    """A crate raise inside the degrade set serves the oracle + one WARN."""

    @pytest.mark.parametrize(
        "exc", [ValueError("v"), RuntimeError("r"), TypeError("t"), OverflowError("o")]
    )
    def test_csv_degrades(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None, exc: Exception
    ) -> None:
        def boom(header: str, rows: object) -> tuple:
            raise exc

        _plant_crate(monkeypatch, crossverify_render_csv=boom)
        with structlog.testing.capture_logs() as logs:
            assert (
                rust_seam.routed_render_csv(
                    CSV_HEADER, [["a"] * 8], python_render=lambda: b"oracle"
                )
                == b"oracle"
            )
        warns = [e for e in logs if e["event"] == "crossverify_rust.fallback"]
        assert warns[0]["reason"] == "csv_render_error"
        assert rust_seam.rust_seam_disabled() is False

    def test_canonical_degrades_warn_once(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        def boom(*args: object) -> None:
            raise AttributeError("broken internals")

        _plant_crate(monkeypatch, crossverify_canonical_identity=boom)
        kwargs: dict[str, object] = {
            "exchange": "NSE",
            "segment": "CASH",
            "lake_symbol": "M&M",
            "fno_underlying": None,
            "fno_expiry_iso": None,
            "fno_is_future": False,
            "fno_exchange": None,
        }
        with structlog.testing.capture_logs() as logs:
            assert rust_seam.routed_canonical_identity(python_oracle=lambda: None, **kwargs) is None
            assert rust_seam.routed_canonical_identity(python_oracle=lambda: None, **kwargs) is None
        warns = [e for e in logs if e["event"] == "crossverify_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "canonical_error"

    def test_s3_key_degrades(self, monkeypatch: pytest.MonkeyPatch, flag_on: None) -> None:
        def boom(*args: object) -> None:
            raise ValueError("bad")

        _plant_crate(monkeypatch, crossverify_s3_key=boom)
        assert (
            rust_seam.routed_s3_key(
                prefix="p",
                day_iso="2026-07-10",
                segment_token="NSE_EQ",
                canonical_symbol="M&M",
                python_oracle=lambda: "oracle-key",
            )
            == "oracle-key"
        )


class TestRefusePoisonGates:
    """Typed refusal codes + malformed returns serve the oracle, warned."""

    def test_csv_typed_refusal_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        _plant_crate(monkeypatch, crossverify_render_csv=lambda header, rows: (None, (4, 0, 2)))
        with structlog.testing.capture_logs() as logs:
            out = rust_seam.routed_render_csv(
                CSV_HEADER, [["a"] * 8], python_render=lambda: b"oracle"
            )
        assert out == b"oracle"
        warns = [e for e in logs if e["event"] == "crossverify_rust.fallback"]
        assert warns[0]["reason"] == "csv_refusal_or_poison"
        assert warns[0]["refusal"] == (4, 0, 2)

    def test_csv_typed_refusal_reraises_exact_python_message(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        """A refusal on genuinely-bad input maps onto the EXISTING
        ExportContractError with the exact pinned bytes — the oracle raises."""
        _plant_crate(monkeypatch, crossverify_render_csv=lambda header, rows: (None, (1, 0, 0)))

        def oracle() -> bytes:
            return csv_render._render_day_csv_python(
                identity=_IDENTITY, rows=[], session=_session()
            )

        with pytest.raises(ExportContractError) as excinfo:
            rust_seam.routed_render_csv(CSV_HEADER, [], python_render=oracle)
        assert "M&M: empty row set" in str(excinfo.value)

    def test_csv_non_bytes_return_refused(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        _plant_crate(monkeypatch, crossverify_render_csv=lambda header, rows: ("str", None))
        assert (
            rust_seam.routed_render_csv(CSV_HEADER, [["a"] * 8], python_render=lambda: b"o") == b"o"
        )

    def test_csv_malformed_return_shape_degrades(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        # A non-2-tuple return fails the unpack with ValueError/TypeError —
        # the degrade set catches it (never a new crash class).
        _plant_crate(monkeypatch, crossverify_render_csv=lambda header, rows: object())
        assert (
            rust_seam.routed_render_csv(CSV_HEADER, [["a"] * 8], python_render=lambda: b"o") == b"o"
        )

    @pytest.mark.parametrize(
        "poison",
        [
            ("a", "b", "c", "d", "e"),  # wrong arity
            ("a", "b", "c", "d", "e", 6),  # non-str member
            ["a", "b", "c", "d", "e", "f"],  # non-tuple
        ],
    )
    def test_canonical_malformed_tuple_refused(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None, poison: object
    ) -> None:
        _plant_crate(monkeypatch, crossverify_canonical_identity=lambda *a: poison)
        with structlog.testing.capture_logs() as logs:
            out = rust_seam.routed_canonical_identity(
                exchange="NSE",
                segment="CASH",
                lake_symbol="M&M",
                fno_underlying=None,
                fno_expiry_iso=None,
                fno_is_future=False,
                fno_exchange=None,
                python_oracle=lambda: ("x",) * 6,
            )
        assert out == ("x",) * 6
        warns = [e for e in logs if e["event"] == "crossverify_rust.fallback"]
        assert warns[0]["reason"] == "canonical_result_refused"

    def test_s3_key_non_str_refused(self, monkeypatch: pytest.MonkeyPatch, flag_on: None) -> None:
        _plant_crate(monkeypatch, crossverify_s3_key=lambda *a: 42)
        with structlog.testing.capture_logs() as logs:
            out = rust_seam.routed_s3_key(
                prefix="p",
                day_iso="2026-07-10",
                segment_token="NSE_EQ",
                canonical_symbol="M&M",
                python_oracle=lambda: "oracle-key",
            )
        assert out == "oracle-key"
        warns = [e for e in logs if e["event"] == "crossverify_rust.fallback"]
        assert warns[0]["reason"] == "s3_key_result_refused"


class TestBurnIn:
    """Per-law 16-call burn-in bit-compare + ship-the-oracle autorevert."""

    def test_csv_burn_in_mismatch_autoreverts(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        _plant_crate(monkeypatch, crossverify_render_csv=lambda header, rows: (b"WRONG", None))
        with structlog.testing.capture_logs() as logs:
            out = rust_seam.routed_render_csv(
                CSV_HEADER, [["a"] * 8], python_render=lambda: b"oracle"
            )
        assert out == b"oracle"
        assert rust_seam.rust_seam_disabled() is True
        errors = [e for e in logs if e["event"] == "crossverify_rust.autoreverted"]
        assert errors[0]["reason"] == "burn_in_mismatch_csv"
        # The latch serves the oracle on every later call, per law family.
        assert (
            rust_seam.routed_render_csv(CSV_HEADER, [["a"] * 8], python_render=lambda: b"o2")
            == b"o2"
        )

    def test_csv_burn_in_countdown_completes(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        rows = [["a"] * 8]
        expected = rust_seam.routed_render_csv  # silence long lines
        _plant_crate(
            monkeypatch,
            crossverify_render_csv=lambda header, cells: (
                (CSV_HEADER + "\n" + ",".join(cells[0]) + "\n").encode(),
                None,
            ),
        )
        oracle = (CSV_HEADER + "\n" + ",".join(rows[0]) + "\n").encode()
        rust_seam.reset_crossverify_rust_for_tests(remaining=2)
        with structlog.testing.capture_logs() as logs:
            for _ in range(3):
                assert expected(CSV_HEADER, rows, python_render=lambda: oracle) == oracle
        done = [e for e in logs if e["event"] == "crossverify_rust.burn_in_complete"]
        assert len(done) == 1
        assert done[0]["law"] == "csv_render"
        engaged = [e for e in logs if e["event"] == "crossverify_rust.engaged"]
        assert len(engaged) == 1

    def test_canonical_burn_in_mismatch_ships_oracle(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        _plant_crate(
            monkeypatch,
            crossverify_canonical_identity=lambda *a: ("WRONG",) * 6,
        )
        oracle_fields = ("a", "b", "c", "d", "e", "f")
        with structlog.testing.capture_logs() as logs:
            out = rust_seam.routed_canonical_identity(
                exchange="NSE",
                segment="CASH",
                lake_symbol="M&M",
                fno_underlying=None,
                fno_expiry_iso=None,
                fno_is_future=False,
                fno_exchange=None,
                python_oracle=lambda: oracle_fields,
            )
        assert out == oracle_fields
        assert rust_seam.rust_seam_disabled() is True
        # NIT-4 pin: the vendor-derived field payloads ride repr() (#907).
        errors = [e for e in logs if e["event"] == "crossverify_rust.autoreverted"]
        assert errors[0]["reason"] == "burn_in_mismatch_canonical"
        assert errors[0]["rust_fields"] == repr(list(("WRONG",) * 6))
        assert errors[0]["python_fields"] == repr(list(oracle_fields))

    def test_canonical_burn_in_none_vs_none_counts_down(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        _plant_crate(monkeypatch, crossverify_canonical_identity=lambda *a: None)
        rust_seam.reset_crossverify_rust_for_tests(remaining=1)
        with structlog.testing.capture_logs() as logs:
            assert (
                rust_seam.routed_canonical_identity(
                    exchange="MCX",
                    segment="CASH",
                    lake_symbol="X",
                    fno_underlying=None,
                    fno_expiry_iso=None,
                    fno_is_future=False,
                    fno_exchange=None,
                    python_oracle=lambda: None,
                )
                is None
            )
        done = [e for e in logs if e["event"] == "crossverify_rust.burn_in_complete"]
        assert done[0]["law"] == "canonical_identity"
        # Steady state (budget exhausted): the crate value ships with NO
        # oracle in the path.
        assert (
            rust_seam.routed_canonical_identity(
                exchange="MCX",
                segment="CASH",
                lake_symbol="X",
                fno_underlying=None,
                fno_expiry_iso=None,
                fno_is_future=False,
                fno_exchange=None,
                python_oracle=lambda: pytest.fail("steady state ran the oracle"),
            )
            is None
        )

    def test_s3_key_burn_in_mismatch_and_countdown(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        _plant_crate(monkeypatch, crossverify_s3_key=lambda *a: "WRONG")
        with structlog.testing.capture_logs() as logs:
            out = rust_seam.routed_s3_key(
                prefix="p",
                day_iso="2026-07-10",
                segment_token="NSE_EQ",
                canonical_symbol="M&M",
                python_oracle=lambda: "right",
            )
        assert out == "right"
        assert rust_seam.rust_seam_disabled() is True
        # NIT-4 pin: the vendor-derived key payloads ride repr() (#907).
        errors = [e for e in logs if e["event"] == "crossverify_rust.autoreverted"]
        assert errors[0]["reason"] == "burn_in_mismatch_s3_key"
        assert errors[0]["rust_key"] == repr("WRONG")
        assert errors[0]["python_key"] == repr("right")
        rust_seam.reset_crossverify_rust_for_tests(remaining=1)
        _plant_crate(monkeypatch, crossverify_s3_key=lambda *a: "right")
        with structlog.testing.capture_logs() as logs:
            assert (
                rust_seam.routed_s3_key(
                    prefix="p",
                    day_iso="2026-07-10",
                    segment_token="NSE_EQ",
                    canonical_symbol="M&M",
                    python_oracle=lambda: "right",
                )
                == "right"
            )
        done = [e for e in logs if e["event"] == "crossverify_rust.burn_in_complete"]
        assert done[0]["law"] == "s3_key"

    def test_steady_state_skips_oracle(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        """remaining=0 → the crate value ships with NO oracle in the path."""
        rust_seam.reset_crossverify_rust_for_tests(remaining=0)
        _plant_crate(monkeypatch, crossverify_s3_key=lambda *a: "rust-key")

        def oracle_bomb() -> str:  # pragma: no cover — detonation IS the failure
            raise AssertionError("steady state ran the oracle")

        assert (
            rust_seam.routed_s3_key(
                prefix="p",
                day_iso="2026-07-10",
                segment_token="NSE_EQ",
                canonical_symbol="M&M",
                python_oracle=oracle_bomb,
            )
            == "rust-key"
        )


class TestWiredLawsFlagOn:
    """The public laws flag-ON over a faithful fake crate — full wiring."""

    def test_render_day_csv_engages_and_matches_oracle(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        _faithful_crate(monkeypatch)
        out = render_day_csv(identity=_IDENTITY, rows=_rows(), session=_session())
        assert out == _oracle_bytes()

    def test_render_day_csv_flag_on_refusals_raise_exact_python_bytes(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        _faithful_crate(monkeypatch)
        with pytest.raises(ExportContractError) as excinfo:
            render_day_csv(identity=_IDENTITY, rows=[], session=_session())
        assert "M&M: empty row set" in str(excinfo.value)
        # A spot row carrying oi refuses from the PYTHON law before any FFI.
        bad = _rows(1)
        bad[0] = ExportRow(
            ts_utc=bad[0].ts_utc,
            open=bad[0].open,
            high=bad[0].high,
            low=bad[0].low,
            close=bad[0].close,
            volume=bad[0].volume,
            oi=5,
        )
        with pytest.raises(ExportContractError, match="spot row carries oi=5"):
            render_day_csv(identity=_IDENTITY, rows=bad, session=_session())

    def test_canonical_identity_flag_on_all_arms(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        received: list[tuple] = []

        def spy_identity(*args: object) -> tuple | None:
            received.append(args)
            exchange, segment, lake_symbol = args[0], args[1], args[2]
            return canonical._identity_fields(
                canonical._canonical_identity_python(
                    exchange=exchange, segment=segment, lake_symbol=lake_symbol
                )
            )

        _plant_crate(monkeypatch, crossverify_canonical_identity=spy_identity)
        # CASH arm — no parse; fno args cross as None/False.
        identity = canonical_identity(exchange="NSE", segment="CASH", lake_symbol="M&M")
        assert identity == _IDENTITY
        assert received[-1] == ("NSE", "CASH", "M&M", None, None, False, None)
        # FNO arm — the PYTHON parse's fields cross the FFI.
        fut = canonical_identity(exchange="NSE", segment="FNO", lake_symbol="NSE-NIFTY-30Jul26-FUT")
        assert fut is not None
        assert fut.canonical_symbol == "NIFTY-2026-07-30-FUT"
        assert received[-1] == (
            "NSE",
            "FNO",
            "NSE-NIFTY-30Jul26-FUT",
            "NIFTY",
            "2026-07-30",
            True,
            "NSE",
        )
        # An unparsed FNO dir crosses as the None trio → None both arms.
        assert canonical_identity(exchange="NSE", segment="FNO", lake_symbol="JUNK") is None
        assert received[-1] == ("NSE", "FNO", "JUNK", None, None, False, None)
        # A parsed OPTION leg crosses with fno_is_future=False → None both arms.
        assert (
            canonical_identity(
                exchange="NSE", segment="FNO", lake_symbol="NSE-NIFTY-30Jul26-24000-CE"
            )
            is None
        )
        assert received[-1] == (
            "NSE",
            "FNO",
            "NSE-NIFTY-30Jul26-24000-CE",
            "NIFTY",
            "2026-07-30",
            False,
            "NSE",
        )
        # Deny-by-default parity: a BSE non-SENSEX index is None flag-ON.
        assert canonical_identity(exchange="BSE", segment="INDEX", lake_symbol="BSEIT") is None

    def test_s3_key_flag_on_matches_oracle(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        _faithful_crate(monkeypatch)
        assert (
            s3_key(prefix="crossverify/groww", day=_DAY, identity=_IDENTITY)
            == "crossverify/groww/2026-07-10/NSE_EQ/M&M.csv"
        )


class TestResetSeam:
    """The test-isolation reset restores latch + budgets + memo."""

    def test_reset_restores_defaults(self) -> None:
        rust_seam._disable("some_reason")
        rust_seam._warn_once("some_warn")
        rust_seam._BURN_IN_REMAINING["csv_render"] = 0
        rust_seam.reset_crossverify_rust_for_tests()
        assert rust_seam.rust_seam_disabled() is False
        assert rust_seam._BURN_IN_REMAINING == {
            "csv_render": rust_seam.BURN_IN_SAMPLES,
            "canonical_identity": rust_seam.BURN_IN_SAMPLES,
            "s3_key": rust_seam.BURN_IN_SAMPLES,
        }
        assert set() == rust_seam._OBSERVABILITY_MEMO
