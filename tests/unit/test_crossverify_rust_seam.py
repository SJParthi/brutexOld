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
from typing import TYPE_CHECKING, Any

import pytest
import structlog.testing
from brutex.calendars.nse import TradingSession, get_session_checked
from brutex.config import rust_strict
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


class _FakeOpsModule(types.ModuleType):
    """A fake ``brutex_ops`` with DECLARED seam attributes.

    The declarations (``Any``) make direct attribute assignment legal under
    pyright — a bare ``types.ModuleType`` refuses assigns to undeclared
    attributes. Runtime behavior is identical to a plain module object.
    """

    __version__: str
    crossverify_render_csv: Any
    crossverify_canonical_identity: Any
    crossverify_s3_key: Any


class _FakePyo3Module(types.ModuleType):
    """A fake ``pyo3_runtime`` with a DECLARED ``PanicException`` slot."""

    PanicException: Any


def _session() -> TradingSession:
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


def _plant_crate(monkeypatch: pytest.MonkeyPatch, **functions: object) -> _FakeOpsModule:
    """Plant a fake ``brutex_ops`` module carrying *functions* (full API by default)."""
    module = _FakeOpsModule("brutex_ops")
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
def _fresh_seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset the seam latch/burn-in/memo around every test (isolation).

    Also opts OUT of strict mode (DEC-RUST-STRICT-NO-FALLBACK-001, which
    ships ON): this module exists to exercise the DEGRADE lattice, so the
    honest move is an explicit opt-out here rather than a weakened
    assertion. ``TestStrictRefusal`` opts back IN per test on THIS SAME
    shared ``monkeypatch`` object.
    """
    monkeypatch.setenv(rust_strict.RUST_STRICT_ENV, "0")
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
        # DEC-RUST-DEFAULT-ON-WAVE-001: unset/blank ⇒ ON (the default pin).
        monkeypatch.delenv(rust_seam.CROSSVERIFY_RUST_ENV, raising=False)
        assert rust_seam.crossverify_rust_enabled() is True
        monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, "   ")
        assert rust_seam.crossverify_rust_enabled() is True

    def test_junk_flag_warns_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, "banana")
        with structlog.testing.capture_logs() as logs:
            assert rust_seam.crossverify_rust_enabled() is False
            assert rust_seam.crossverify_rust_enabled() is False
        warns = [e for e in logs if e["event"] == "crossverify_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"

    def test_vocabulary_equality_pinned_to_engine(self) -> None:
        """The LOCAL twin must never drift from the engine vocabulary.

        The engine package hard-imports the ``fno_math`` wheel (the exact
        reason the seam carries a LOCAL twin — module docstring), so this
        pin skips LOUDLY where that wheel is absent: the ops-rust-parity
        lane installs only ``brutex_ops`` (its job is real-crate drift,
        not the engine). NOT a vacuous-green risk (the MINOR-1 class):
        the pin's guaranteed executing home is main CI's ci.yml rust_seam
        stanza, whose ``test`` job always installs the fno_math wheel —
        and a missing wheel THERE fails hundreds of engine suites loudly.
        """
        sweep_lookahead = pytest.importorskip(
            "brutex.engine.sweep_lookahead",
            reason="fno_math wheel absent (the ops-rust-parity lane) — the "
            "engine-vocabulary equality pin executes in main CI's test job",
        )

        assert rust_seam._TRUTHY == sweep_lookahead._TRUTHY
        assert rust_seam._FALSY == sweep_lookahead._FALSY
        for raw in ("1", "true", "YES", " on ", "0", "false", "No", " OFF ", "", "  ", "junk"):
            assert rust_seam.env_flag_tristate(raw) == sweep_lookahead.env_flag_tristate(raw)


class TestFlagOffNeverTouchesCrate:
    """The explicit "0" kill switch is byte-identical AND crate-untouched (the bomb spy).

    Retargeted from the pre-flip unset arm (DEC-RUST-DEFAULT-ON-WAVE-001);
    assertions unchanged.
    """

    def test_all_three_laws_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, "0")

        def bomb() -> None:  # pragma: no cover — detonation IS the failure
            raise AssertionError("flag-OFF touched the crate resolver")

        monkeypatch.setattr(rust_seam, "_resolve_crate", bomb)

        class _RecordingSpy(types.ModuleType):
            # A RECORDING spy, not a raising bomb: CodeQL
            # py/unexpected-raise-in-special-method fires on ANY
            # non-AttributeError raised from __getattr__, and raising
            # AttributeError alone would be ordinary fallback semantics (a
            # silently defused bomb — the seam's degrade set even catches
            # it). Recording the access FIRST then raising the normal
            # AttributeError keeps STRICTLY STRONGER teeth: a crate touch is
            # detected by the explicit empty-accesses assert below even if
            # the raise is swallowed by any guard. Dunder lookups are
            # excluded — interpreter introspection probes are not crate
            # touches; the seam handshake reads only the three plain-named
            # callables.
            accesses: list[str]

            def __init__(self, name: str) -> None:
                super().__init__(name)
                self.accesses = []

            def __getattr__(self, name: str) -> object:  # pragma: no cover
                if not (name.startswith("__") and name.endswith("__")):
                    self.accesses.append(name)
                raise AttributeError(name)

        spy = _RecordingSpy("brutex_ops")
        monkeypatch.setitem(sys.modules, "brutex_ops", spy)
        assert render_day_csv(identity=_IDENTITY, rows=_rows(), session=_session()) == (
            _oracle_bytes()
        )
        identity = canonical_identity(exchange="NSE", segment="CASH", lake_symbol="M&M")
        assert identity == _IDENTITY
        assert (
            s3_key(prefix="crossverify/groww", day=_DAY, identity=_IDENTITY)
            == "crossverify/groww/2026-07-10/NSE_EQ/M&M.csv"
        )
        # THE teeth: flag-OFF touched NO brutex_ops attribute — detected by
        # the recording even if a future guard were to swallow the raise.
        assert spy.accesses == []

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

        def fake_import(name: str, *args: str | None) -> types.ModuleType:
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
        module = _FakeOpsModule("brutex_ops")
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

        def fake_import(name: str, *args: str | None) -> types.ModuleType:
            if name == "pyo3_runtime":
                raise ImportError("absent")
            return real_import(name, *args)  # pragma: no cover — no other import fires

        monkeypatch.setattr(rust_seam.importlib, "import_module", fake_import)
        assert rust_seam._panic_belt_errors() == (Exception,)

    def test_panic_class_joins_belt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class PanicException(BaseException):
            pass

        module = _FakePyo3Module("pyo3_runtime")
        module.PanicException = PanicException
        monkeypatch.setitem(sys.modules, "pyo3_runtime", module)
        assert rust_seam._panic_belt_errors() == (Exception, PanicException)

    def test_non_type_or_exception_alias_excluded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _FakePyo3Module("pyo3_runtime")
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

        panic_module = _FakePyo3Module("pyo3_runtime")
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

        panic_module = _FakePyo3Module("pyo3_runtime")
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
            python_oracle=lambda: ("o", "o", "o", "o", "o", "o"),
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
        kwargs: dict[str, Any] = {
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
                python_oracle=lambda: ("x", "x", "x", "x", "x", "x"),
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

        def spy_identity(*args: Any) -> tuple | None:
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


class TestStrictRefusal:
    """DEC-RUST-STRICT-NO-FALLBACK-001 — the two announcement choke points refuse.

    This facade funnels EVERY Python fall-back through exactly two
    functions — ``_warn_once`` (the warn-once memo) and ``_disable`` (the
    process latch) — and both call ``_refuse_if_strict`` BEFORE their
    memo/latch write, so a latched early-return is structurally
    unreachable under strict mode.

    CLASS C is EMPTY here: every reason token this seam can emit is
    either a crate fault (CLASS A) or a structurally-refused Rust RESULT
    (CLASS B). There is no reason whose trigger is a legitimate input the
    crate was never meant to handle — the one input-shaped token,
    ``csv_refusal_or_poison``, fires only for row shapes the production
    call site (``csv_render._validated_cell_rows``) has already rejected,
    so reaching it means the crate disagreed with a validated input.
    Hence both choke points call ``refuse_if_strict`` UNCONDITIONALLY
    (no ``_STRICT_EXEMPT_REASONS`` frozenset — an empty one would leave
    an unreachable branch under the 100% branch gate).
    """

    @staticmethod
    def _strict_on(monkeypatch: pytest.MonkeyPatch) -> None:
        """Arm strict mode on the SHARED monkeypatch the autouse fixture used.

        Never a nested ``MonkeyPatch.context()`` layered on top of the
        fixture's own object: two objects tear down LIFO and would leak
        ``BRUTEX_RUST_STRICT`` past this module.
        """
        monkeypatch.delenv(rust_strict.RUST_STRICT_ENV, raising=False)
        assert rust_strict.strict_mode() is True

    @staticmethod
    def _bomb_import(monkeypatch: pytest.MonkeyPatch) -> None:
        """Make the ``brutex_ops`` import raise — the CLASS-A wheel-absent path."""
        monkeypatch.delitem(sys.modules, "brutex_ops", raising=False)
        real_import = rust_seam.importlib.import_module

        def fake_import(name: str, *args: str | None) -> types.ModuleType:
            if name == "brutex_ops":
                raise ImportError("no wheel")
            return real_import(name, *args)  # pragma: no cover — no other import fires

        monkeypatch.setattr(rust_seam.importlib, "import_module", fake_import)

    # ---- CLASS A ----------------------------------------------------

    def test_class_a_crate_unavailable_refuses(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        """Wheel absent ⇒ ``RustFallbackRefused``, and the latch stays CLEAR.

        The clear latch is the proof the refusal fires BEFORE the latch
        write, which is what makes the wiring complete rather than a
        sample: no later call can take a silent latched early-return.
        """
        self._strict_on(monkeypatch)
        self._bomb_import(monkeypatch)
        with pytest.raises(rust_strict.RustFallbackRefused) as excinfo:
            render_day_csv(identity=_IDENTITY, rows=_rows(), session=_session())
        message = str(excinfo.value)
        assert "brutex.crossverify.rust_seam" in message
        assert "crate_unavailable" in message
        assert rust_seam.rust_seam_disabled() is False

    def test_class_a_crate_unavailable_serves_oracle_when_strict_off(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        """The SAME path with strict OFF degrades to the oracle bytes."""
        monkeypatch.setenv(rust_strict.RUST_STRICT_ENV, "0")
        self._bomb_import(monkeypatch)
        assert render_day_csv(identity=_IDENTITY, rows=_rows(), session=_session()) == (
            _oracle_bytes()
        )
        assert rust_seam.rust_seam_disabled() is True

    def test_class_a_flag_unrecognised_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A junk flag value is a crate-selection fault, not an input shape."""
        self._strict_on(monkeypatch)
        monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, "banana")
        with pytest.raises(rust_strict.RustFallbackRefused, match="flag_unrecognised"):
            rust_seam.crossverify_rust_enabled()

    def test_class_a_crate_stale_refuses(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        """A pre-lane wheel (missing API members) refuses instead of latching."""
        self._strict_on(monkeypatch)
        module = _FakeOpsModule("brutex_ops")
        module.crossverify_render_csv = lambda header, rows: (b"", None)
        monkeypatch.setitem(sys.modules, "brutex_ops", module)
        with pytest.raises(rust_strict.RustFallbackRefused, match="crate_stale"):
            rust_seam.routed_s3_key(
                prefix="p",
                day_iso="2026-07-10",
                segment_token="NSE_EQ",
                canonical_symbol="M&M",
                python_oracle=lambda: "oracle-key",
            )
        assert rust_seam.rust_seam_disabled() is False

    # ---- CLASS B ----------------------------------------------------

    def test_class_b_result_refused_refuses(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        """A structurally-refused Rust RESULT is a crate answer, not an input."""
        self._strict_on(monkeypatch)
        _plant_crate(monkeypatch, crossverify_s3_key=lambda *args: 7)
        with pytest.raises(rust_strict.RustFallbackRefused, match="s3_key_result_refused"):
            rust_seam.routed_s3_key(
                prefix="p",
                day_iso="2026-07-10",
                segment_token="NSE_EQ",
                canonical_symbol="M&M",
                python_oracle=lambda: "oracle-key",
            )

    def test_class_b_burn_in_mismatch_refuses(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        """A burn-in bit-compare mismatch refuses BEFORE the autorevert latch."""
        self._strict_on(monkeypatch)
        _plant_crate(monkeypatch, crossverify_render_csv=lambda header, rows: (b"WRONG", None))
        with pytest.raises(rust_strict.RustFallbackRefused, match="burn_in_mismatch_csv"):
            rust_seam.routed_render_csv(CSV_HEADER, [["a"] * 8], python_render=lambda: b"oracle")
        assert rust_seam.rust_seam_disabled() is False

    # ---- CLASS C (structurally empty) -------------------------------

    def test_no_class_c_exemption_frozenset_exists(self) -> None:
        """Pin the structural absence of a CLASS-C carve-out on this facade.

        If a future reason token genuinely needs an exemption it must be
        introduced deliberately (with a per-token comment naming the input
        shape) — this pin makes the silent growth of a loophole fail.
        """
        assert not hasattr(rust_seam, "_STRICT_EXEMPT_REASONS")
        assert not hasattr(rust_seam, "_STRICT_UNRESOLVED_REASONS")


class TestCrateMemo:
    """The resolved-crate memo: what it caches, what it must NOT, and the
    reset that keeps every crate swap in this file visible.

    Motivation (measured on the sibling ``money_rust``,
    DEC-MONEY-RUST-WRAPPER-COST-001): the resolve cost 911 ns/call because
    ``importlib.import_module`` is ~574 ns even on a ``sys.modules`` HIT and
    the symbol handshake the rest. This seam pays it per routed call.

    Self-contained by design: it builds its fake crate from the facade's OWN
    API tuple and drives ``_resolve_crate`` directly, so it needs nothing
    from this file's routed-call harness.
    """

    @staticmethod
    def _mod() -> object:
        from brutex.crossverify import rust_seam as _memo_cv

        return _memo_cv

    @classmethod
    def _fake(cls, *, drop: str | None = None) -> object:
        import types as _t

        mod = cls._mod()
        crate = _t.SimpleNamespace()
        for name in mod._CRATE_API:  # type: ignore[attr-defined]
            if name != drop:
                setattr(crate, name, lambda *a, **k: None)
        return crate

    @classmethod
    def _route_import(cls, monkeypatch: pytest.MonkeyPatch, crate: object) -> list[str]:
        """Point the facade's importer at *crate*; return the call log."""
        import importlib as _il

        mod = cls._mod()
        calls: list[str] = []
        real = _il.import_module

        def _import(name: str, package: str | None = None) -> object:
            if name == "brutex_ops":
                calls.append(name)
                return crate
            return real(name, package)

        monkeypatch.setattr(mod.importlib, "import_module", _import)  # type: ignore[attr-defined]
        return calls

    @classmethod
    def _strict_off(cls, monkeypatch: pytest.MonkeyPatch) -> None:
        """This class exercises the DEGRADE lattice — pin strict OFF itself.

        Self-contained rather than relying on a module-level fixture, so the
        failed-resolve pin below cannot be silently turned into a strict-mode
        refusal by an unrelated harness change.
        """
        from brutex.config import rust_strict

        monkeypatch.setenv(rust_strict.RUST_STRICT_ENV, "0")

    def test_success_is_memoized_and_import_runs_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = self._mod()
        crate = self._fake()
        calls = self._route_import(monkeypatch, crate)
        mod.reset_crossverify_rust_for_tests()  # type: ignore[attr-defined]
        for _ in range(5):
            assert mod._resolve_crate() is crate  # type: ignore[attr-defined]
        assert calls == ["brutex_ops"], "the crate must be imported exactly ONCE"
        assert mod._CRATE_MEMO[0] is crate  # type: ignore[attr-defined]

    def test_failed_resolve_is_never_memoized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A FAILED resolve must NOT be cached: `_disable` REFUSES before it
        # latches under strict mode, so caching failure would manufacture the
        # silently-latched steady state strict mode exists to forbid — and a
        # wheel rebuilt mid-process could never be picked up.
        mod = self._mod()
        self._strict_off(monkeypatch)
        self._route_import(monkeypatch, self._fake(drop="crossverify_render_csv"))
        mod.reset_crossverify_rust_for_tests()  # type: ignore[attr-defined]
        assert mod._resolve_crate() is None  # type: ignore[attr-defined]
        assert mod._CRATE_MEMO[0] is None  # type: ignore[attr-defined]

    def test_reset_clears_the_memo_so_a_crate_swap_is_seen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # THE LOAD-BEARING PIN. This file's crate installers swap WITHOUT
        # resetting, so per-test isolation rests on the reset seam clearing
        # the memo (this file's autouse fixture calls it).
        mod = self._mod()
        first = self._fake()
        self._route_import(monkeypatch, first)
        mod.reset_crossverify_rust_for_tests()  # type: ignore[attr-defined]
        assert mod._resolve_crate() is first  # type: ignore[attr-defined]
        second = self._fake()
        self._route_import(monkeypatch, second)
        assert mod._resolve_crate() is first, "warm memo still serves — no re-import"  # type: ignore[attr-defined]
        mod.reset_crossverify_rust_for_tests()  # type: ignore[attr-defined]
        assert mod._resolve_crate() is second  # type: ignore[attr-defined]

    def test_memo_holds_the_module_not_a_bound_callable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Every routed law reads `getattr(crate, name)` per call, so an
        # ATTRIBUTE monkeypatch on the memoized crate must still be seen —
        # a memo holding a bound callable would serve the pre-patch function
        # forever, silently breaking ~85 attribute-monkeypatch test sites.
        mod = self._mod()
        crate = self._fake()
        self._route_import(monkeypatch, crate)
        mod.reset_crossverify_rust_for_tests()  # type: ignore[attr-defined]
        assert mod._resolve_crate() is crate  # type: ignore[attr-defined]
        sentinel = object()
        monkeypatch.setattr(crate, "crossverify_render_csv", lambda *a, **k: sentinel)
        served = mod._resolve_crate().crossverify_render_csv  # type: ignore[attr-defined]
        assert served() is sentinel
