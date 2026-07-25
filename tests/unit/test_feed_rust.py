"""Unit gate for :mod:`brutex.feed_rust` (DEC-FEED-RUST-MATERIALIZE-002) —
100% line+branch STANDALONE.

Covers the retired-axis flag law (``require_materialize_flags`` — unset /
empty / truthy proceed; explicit falsy / junk are a loud typed
:class:`~brutex.feed.errors.FeedError` naming the DEC), the wheel-REQUIRED
resolver (``require_feed_kernel`` — real wheel resolves; a poisoned import
raises the remediation-naming refusal, never a silent degrade), EVERY
violated-law token of the refuse-poison ``validate_strict_scan`` gate, the
leaf-import AST pin, the ``_TRUTHY``/``_FALSY`` mirror pin against
``sweep_lookahead``'s vocabulary, and — wheel-gated — the integers-only
strict-scan FFI shape, the ``BarrierCore`` verdict codes, the series
decision arithmetic, and the shim freshness handshake.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest
from brutex import feed_rust
from brutex.feed_rust import (
    FEED_RUST_ENV,
    MATERIALIZE_DECISION,
    MATERIALIZE_RUST_ENV,
    STOP_NONFINITE,
    STOP_NONPOSITIVE,
    WHEEL_REMEDIATION,
    require_feed_kernel,
    require_materialize_flags,
    validate_strict_scan,
)

_WHEEL_SKIP_REASON = "brutex_feed wheel not installed — build via maturin"

#: A clean regular-session anchor: 2024-01-02 09:15 IST == 03:45 UTC, in µs.
_SESSION_1_US = 1_704_167_100_000_000

#: One IST day in epoch microseconds.
_DAY_US = 86_400_000_000

#: The crate's fixed-IST magnitude envelope ceiling
#: (``rust/brutex-feed/src/scan.rs`` ``MAX_SAFE_TS_US``, half-open) — the
#: refusal boundary of ``DEC-FFI-MAGNITUDE-CEILING-001``. MIRRORED, not
#: imported: adding a Rust const to the FFI handshake surface for a test's
#: convenience would grow ``_EXPECTED_COMPILED_SYMBOLS`` +
#: ``EXPECTED_BUILD.json`` + the ``.pyi`` in lockstep for zero production
#: value. ``test_max_safe_ts_us_mirror_matches_crate_source`` is the drift pin.
_MAX_SAFE_TS_US = 253_402_214_400_000_000

#: One minute in epoch microseconds.
_MINUTE_US = 60_000_000


@pytest.fixture(autouse=True)
def _clean_flag_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip BOTH flag env vars so ambient operator env can never leak in."""
    monkeypatch.delenv(MATERIALIZE_RUST_ENV, raising=False)
    monkeypatch.delenv(FEED_RUST_ENV, raising=False)


def _feed_error_type() -> type[Exception]:
    """The typed refusal class — imported the consumers' way."""
    from brutex.feed.errors import FeedError

    return FeedError


# ── The retired-axis flag law ───────────────────────────────────────────────


class TestRequireMaterializeFlags:
    """``require_materialize_flags`` — unset/empty/truthy proceed, SET
    falsy/junk refuse loudly (the ``BRUTEX_FILL_MODE`` retired-axis law)."""

    def test_unset_both_proceeds(self) -> None:
        require_materialize_flags()  # the autouse fixture stripped both

    @pytest.mark.parametrize("raw", ["1", "TRUE", " on ", "yes", "On", "  true  "])
    @pytest.mark.parametrize("env", [MATERIALIZE_RUST_ENV, FEED_RUST_ENV])
    def test_explicit_truthy_proceeds(
        self, env: str, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(env, raw)
        require_materialize_flags()

    @pytest.mark.parametrize("raw", ["", "   "])
    @pytest.mark.parametrize("env", [MATERIALIZE_RUST_ENV, FEED_RUST_ENV])
    def test_empty_and_whitespace_proceed(
        self, env: str, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(env, raw)
        require_materialize_flags()

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "FALSE", " Off "])
    @pytest.mark.parametrize("env", [MATERIALIZE_RUST_ENV, FEED_RUST_ENV])
    def test_explicit_falsy_refuses_naming_the_deleted_arm(
        self, env: str, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(env, raw)
        with pytest.raises(
            _feed_error_type(),
            match="asks for the DELETED Python feed-materialization arm",
        ) as excinfo:
            require_materialize_flags()
        message = str(excinfo.value)
        assert env in message
        assert MATERIALIZE_DECISION in message
        assert f"unset {env}" in message

    @pytest.mark.parametrize("env", [MATERIALIZE_RUST_ENV, FEED_RUST_ENV])
    def test_junk_refuses_as_unrecognised(self, env: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(env, "bananas")
        with pytest.raises(_feed_error_type(), match="unrecognised") as excinfo:
            require_materialize_flags()
        message = str(excinfo.value)
        assert env in message
        assert MATERIALIZE_DECISION in message
        assert f"unset {env}" in message

    def test_refusal_is_the_typed_feed_error_family(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.feed.errors import FeedError
        from brutex.models import BruteXError

        monkeypatch.setenv(MATERIALIZE_RUST_ENV, "off")
        with pytest.raises(FeedError) as excinfo:
            require_materialize_flags()
        assert isinstance(excinfo.value, FeedError)
        assert isinstance(excinfo.value, BruteXError)
        assert issubclass(FeedError, BruteXError)


# ── The wheel-REQUIRED resolver ─────────────────────────────────────────────


class TestRequireFeedKernel:
    """``require_feed_kernel`` — real wheel resolves; a broken import is a
    loud typed refusal naming the rebuild remediation."""

    def test_real_wheel_resolves_with_strict_scan(self) -> None:
        pytest.importorskip("brutex_feed", reason=_WHEEL_SKIP_REASON)
        kernel = require_feed_kernel()
        assert hasattr(kernel, "scan_bars_strict")

    def test_missing_wheel_refuses_naming_the_remediation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Poisoning sys.modules with None makes `import brutex_feed` raise
        # ImportError even when the real wheel IS installed.
        monkeypatch.setitem(sys.modules, "brutex_feed", None)
        with pytest.raises(
            _feed_error_type(), match="REQUIRED for feed materialization"
        ) as excinfo:
            require_feed_kernel()
        message = str(excinfo.value)
        assert "maturin develop" in message
        assert WHEEL_REMEDIATION in message
        assert MATERIALIZE_DECISION in message

    def test_flag_refusal_runs_before_the_wheel_import(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # BOTH poisons armed: the flag law must fire FIRST, so a set-falsy
        # flag refuses with ITS message even on a wheel-less box.
        monkeypatch.setitem(sys.modules, "brutex_feed", None)
        monkeypatch.setenv(MATERIALIZE_RUST_ENV, "0")
        with pytest.raises(
            _feed_error_type(),
            match="asks for the DELETED Python feed-materialization arm",
        ) as excinfo:
            require_feed_kernel()
        assert "REQUIRED for feed materialization" not in str(excinfo.value)


# ── validate_strict_scan — the refuse-poison gate, every token ──────────────


class TestValidateStrictScanValid:
    """Well-formed strict results — ``None`` (valid)."""

    def test_clean_scan_with_finish_event_at_n_rows(self) -> None:
        # A finish event anchors AT n_rows when stop is None.
        assert validate_strict_scan(([0, 2, 3], [(4, 0, 1, 0, 1)], None), 4) is None

    def test_stop_with_kept_and_warns_strictly_before(self) -> None:
        assert validate_strict_scan(([0, 1], [(1, 0, 2, 0, 1)], (3, 2, 0)), 4) is None

    def test_empty_result_zero_rows(self) -> None:
        assert validate_strict_scan(([], [], None), 0) is None

    @pytest.mark.parametrize("kind", [STOP_NONPOSITIVE, STOP_NONFINITE])
    def test_both_stop_kinds_valid(self, kind: int) -> None:
        assert validate_strict_scan(([], [], (0, 1, kind)), 1) is None


class TestValidateStrictScanTokens:
    """One test per violated-law token — the exact token is the contract
    (the wheel-required caller weaves it into its raise)."""

    def test_result_shape(self) -> None:
        assert validate_strict_scan([[], [], None], 4) == "result_shape"
        assert validate_strict_scan(([], []), 4) == "result_shape"

    def test_column_shape(self) -> None:
        assert validate_strict_scan(((0, 1), [], None), 4) == "column_shape"
        assert validate_strict_scan(([], ((4, 0, 1, 0, 1),), None), 4) == "column_shape"

    def test_stop_shape(self) -> None:
        assert validate_strict_scan(([], [], [2, 1, 0]), 4) == "stop_shape"
        assert validate_strict_scan(([], [], (2, 1)), 4) == "stop_shape"

    def test_stop_types(self) -> None:
        assert validate_strict_scan(([], [], (2.0, 1, 0)), 4) == "stop_types"
        assert validate_strict_scan(([], [], (2, "close", 0)), 4) == "stop_types"

    def test_stop_range(self) -> None:
        assert validate_strict_scan(([], [], (4, 0, 0)), 4) == "stop_range"
        assert validate_strict_scan(([], [], (-1, 0, 0)), 4) == "stop_range"
        assert validate_strict_scan(([], [], (0, 4, 0)), 4) == "stop_range"
        assert validate_strict_scan(([], [], (0, -1, 0)), 4) == "stop_range"

    def test_stop_kind(self) -> None:
        assert validate_strict_scan(([], [], (0, 0, 2)), 4) == "stop_kind"
        assert validate_strict_scan(([], [], (0, 0, -1)), 4) == "stop_kind"

    def test_kept_type(self) -> None:
        assert validate_strict_scan(([0, 1.0], [], None), 4) == "kept_type"

    def test_kept_order(self) -> None:
        assert validate_strict_scan(([1, 1], [], None), 4) == "kept_order"
        assert validate_strict_scan(([2, 1], [], None), 4) == "kept_order"
        # A negative index would silently WRAP Python list selection.
        assert validate_strict_scan(([-1, 0], [], None), 4) == "kept_order"
        assert validate_strict_scan(([4], [], None), 4) == "kept_order"
        # With a stop, nothing kept AT/after the stop row.
        assert validate_strict_scan(([2], [], (2, 0, 0)), 4) == "kept_order"

    def test_event_shape(self) -> None:
        assert validate_strict_scan(([], [[4, 0, 1, 0, 1]], None), 4) == "event_shape"
        assert validate_strict_scan(([], [(4, 0, 1, 0)], None), 4) == "event_shape"

    def test_event_types(self) -> None:
        assert validate_strict_scan(([], [(4, 0, 1.0, 0, 1)], None), 4) == "event_types"

    def test_event_order(self) -> None:
        dup = [(2, 0, 1, 0, 1), (2, 0, 1, 0, 1)]
        assert validate_strict_scan(([0, 2], dup, None), 4) == "event_order"
        assert validate_strict_scan(([], [(-1, 0, 1, 0, 1)], None), 4) == "event_order"
        # emit_at_row may equal n_rows (finish) but never exceed it.
        assert validate_strict_scan(([], [(5, 0, 1, 0, 1)], None), 4) == "event_order"
        # With a stop, NO event may anchor at/after the stop row — a finish
        # event (emit == n_rows) is structurally impossible under a stop.
        assert validate_strict_scan(([0], [(3, 0, 1, 0, 1)], (3, 0, 0)), 4) == "event_order"

    def test_session_first_range(self) -> None:
        assert validate_strict_scan(([], [(4, 4, 1, 0, 1)], None), 4) == "session_first_range"
        assert validate_strict_scan(([], [(4, -1, 1, 0, 1)], None), 4) == "session_first_range"
        # Under a stop at row 3, a session_first_row AT the stop row is
        # kernel-impossible even though it is < n_rows.
        assert validate_strict_scan(([0], [(2, 3, 1, 0, 1)], (3, 0, 0)), 5) == (
            "session_first_range"
        )

    def test_skipped_floor(self) -> None:
        assert validate_strict_scan(([], [(4, 0, 0, 0, 1)], None), 4) == "skipped_floor"

    def test_offender_range(self) -> None:
        # prev !< skip / skip >= bound / negative prev — one token each way.
        assert validate_strict_scan(([], [(4, 0, 1, 1, 1)], None), 4) == "offender_range"
        assert validate_strict_scan(([], [(4, 0, 1, 0, 4)], None), 4) == "offender_range"
        assert validate_strict_scan(([], [(4, 0, 1, -1, 1)], None), 4) == "offender_range"
        # first_skip_row AT the stop row refuses even though it is < n_rows.
        assert validate_strict_scan(([0], [(2, 0, 1, 0, 3)], (3, 0, 0)), 5) == ("offender_range")


# ── Leaf discipline + vocabulary mirror pins ────────────────────────────────


class TestLeafDiscipline:
    """AST pins: import leaf-ness + the inline flag vocabulary mirror."""

    def test_max_safe_ts_us_mirror_matches_crate_source(self) -> None:
        """``_MAX_SAFE_TS_US`` mirrors the crate const it stands in for.

        The drift pin for ``DEC-FFI-MAGNITUDE-CEILING-001``'s refusal
        boundary. The Python-side value is a MIRROR (see the constant's own
        comment for why it is not on the FFI handshake surface), so a change to
        ``scan.rs``'s envelope has to move this literal too — otherwise the
        guard's boundary test would silently drift off the real boundary and
        stop proving anything.

        Source-parse only, so it runs WITHOUT the wheel — including in the
        wheel-free lanes where the sibling FFI tests skip.
        """
        scan_rs = (Path(__file__).resolve().parents[2] / "rust/brutex-feed/src/scan.rs").read_text(
            encoding="utf-8"
        )
        match = re.search(
            r"^pub const MAX_SAFE_TS_US:\s*i64\s*=\s*([0-9_]+)\s*;",
            scan_rs,
            re.MULTILINE,
        )
        assert match is not None, (
            "rust/brutex-feed/src/scan.rs must declare `pub const MAX_SAFE_TS_US: i64 = …` "
            "— the FFI magnitude envelope this file mirrors "
            "(DEC-FFI-MAGNITUDE-CEILING-001)"
        )
        assert int(match.group(1).replace("_", "")) == _MAX_SAFE_TS_US, (
            "the crate's MAX_SAFE_TS_US moved; update _MAX_SAFE_TS_US in this file "
            "so the envelope-refusal boundary test still pins the REAL boundary"
        )

    def test_leaf_imports_only_from_the_allowlist(self) -> None:
        # Import ALLOWLIST (strictly stronger than a denylist) — a stray
        # third-party or brutex.* import into the leaf fails by construction.
        # MODULE-scope surface: stdlib only (__future__/os/typing) — the
        # cycle-relevant plane (brutex.feed.replay imports this module, so a
        # module-scope brutex.feed edge would be circular; structlog is GONE
        # with the warn-once registry, DEC-FEED-RUST-MATERIALIZE-002).
        # FUNCTION-scope additionally admits the lazy wheel import
        # (brutex_feed) and the lazy `brutex.feed.errors` import inside
        # `_feed_error` (in-function is cycle-safe: every caller reaches it
        # only after the package is fully imported; the typed FeedError
        # family is the wheel-required refusal contract).
        module_allowed = {"__future__", "os", "typing"}
        lazy_allowed = module_allowed | {"brutex_feed", "brutex.feed.errors"}
        source = Path(feed_rust.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        module_level_imports = {
            id(node) for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    allowed = module_allowed if id(node) in module_level_imports else lazy_allowed
                    assert alias.name.split(".")[0] in allowed or alias.name in allowed, alias.name
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, "relative imports forbidden in the leaf"
                module = node.module or ""
                allowed = module_allowed if id(node) in module_level_imports else lazy_allowed
                assert module.split(".")[0] in allowed or module in allowed, node.module

    def test_truthy_falsy_mirror_sweep_lookahead(self) -> None:
        # The leaf may not IMPORT env_flag_tristate (verified cycle:
        # engine.sweep_lookahead imports brutex.feed.lookahead at module
        # scope) — so its inline vocabulary is mirror-PINNED instead.
        from brutex.engine import sweep_lookahead

        assert feed_rust._TRUTHY == sweep_lookahead._TRUTHY  # pyright: ignore[reportPrivateUsage]
        assert feed_rust._FALSY == sweep_lookahead._FALSY  # pyright: ignore[reportPrivateUsage]


# ── Wheel-gated FFI pins ────────────────────────────────────────────────────


class TestStrictScanFfi:
    """Integers-only strict-scan FFI shape + the refusal edges (needs the
    wheel)."""

    @pytest.fixture
    def kernel_module(self) -> object:
        """The installed wheel, or a loud skip naming the rebuild remedy."""
        return pytest.importorskip("brutex_feed", reason=_WHEEL_SKIP_REASON)

    def test_result_is_integers_only_with_warn_and_stop(self, kernel_module: object) -> None:
        # Session 1 carries a x10 spike (skipped; the WARN flushes at the
        # NEXT session's first row); session 2 stops on a non-positive open.
        # Walk every kept/warn/stop leaf: all ints — no float, no string,
        # no timestamp ever crosses.
        ts = [
            _SESSION_1_US,
            _SESSION_1_US + _MINUTE_US,
            _SESSION_1_US + 2 * _MINUTE_US,
            _SESSION_1_US + _DAY_US,
            _SESSION_1_US + _DAY_US + _MINUTE_US,
        ]
        result = kernel_module.scan_bars_strict(  # type: ignore[attr-defined]
            ts,
            [100.0, 100.0, 100.0, 100.0, -1.0],
            [101.0, 101.0, 101.0, 101.0, 1.0],
            [99.0, 99.0, 99.0, 99.0, 1.0],
            [100.0, 1000.0, 100.5, 100.0, 1.0],
            True,
            4.0,
        )
        assert result == ([0, 2, 3], [(3, 0, 1, 0, 1)], (4, 0, STOP_NONPOSITIVE))
        assert validate_strict_scan(result, 5) is None
        kept, warns, stop = result
        leaves: list[object] = list(kept)
        for event in warns:
            leaves.extend(event)
        assert stop is not None
        leaves.extend(stop)
        assert leaves, "the walked result must not be trivially empty"
        for leaf in leaves:
            assert type(leaf) is int, repr(leaf)

    def test_positivity_stop_shape_same_session(self, kernel_module: object) -> None:
        # The 4-row same-session shape: the spike's pending WARN is
        # truncated by the stop (nothing recorded at/after the stop row).
        ts = [_SESSION_1_US + k * _MINUTE_US for k in range(4)]
        result = kernel_module.scan_bars_strict(  # type: ignore[attr-defined]
            ts,
            [100.0, 100.0, 100.0, -1.0],
            [101.0, 101.0, 101.0, 1.0],
            [99.0, 99.0, 99.0, 1.0],
            [100.0, 1000.0, 100.5, 1.0],
            True,
            4.0,
        )
        assert result == ([0, 2], [], (3, 0, STOP_NONPOSITIVE))

    def test_nan_high_stops_nonfinite(self, kernel_module: object) -> None:
        result = kernel_module.scan_bars_strict(  # type: ignore[attr-defined]
            [_SESSION_1_US], [1.0], [float("nan")], [1.0], [1.0], True, 4.0
        )
        assert result == ([], [], (0, 1, STOP_NONFINITE))

    def test_length_mismatch_raises_value_error(self, kernel_module: object) -> None:
        with pytest.raises(ValueError, match="price column lengths diverge"):
            kernel_module.scan_bars_strict(  # type: ignore[attr-defined]
                [], [1.0], [1.0, 2.0], [1.0], [1.0], False, 4.0
            )
        with pytest.raises(ValueError, match="ts_us length"):
            kernel_module.scan_bars_strict(  # type: ignore[attr-defined]
                [], [1.0], [1.0], [1.0], [1.0], True, 4.0
            )
        with pytest.raises(ValueError, match="must be empty"):
            kernel_module.scan_bars_strict(  # type: ignore[attr-defined]
                [_SESSION_1_US], [1.0], [1.0], [1.0], [1.0], False, 4.0
            )

    def test_out_of_envelope_ts_abstains_none(self, kernel_module: object) -> None:
        assert (
            kernel_module.scan_bars_strict(  # type: ignore[attr-defined]
                [-1], [1.0], [1.0], [1.0], [1.0], True, 4.0
            )
            is None
        )


class TestBarrierCoreFfi:
    """The P-06 ``BarrierCore`` verdict codes + cursor state machine."""

    def test_verdict_codes_and_state(self) -> None:
        brutex_feed = pytest.importorskip("brutex_feed", reason=_WHEEL_SKIP_REASON)
        b = brutex_feed.BarrierCore(True)
        assert b.access(0) == 1  # no cursor armed yet
        assert b.advance_to(-1) == 1  # negative advance refused
        assert b.advance_to(5) == 0
        assert b.access(-1) == 2  # negative index
        assert b.access(6) == 3  # ahead of the cursor
        assert b.access(5) == 0  # at the cursor — legal
        assert b.touched() == [5]
        assert b.max_touched() == 5
        assert b.current_index == 5
        assert b.records is True
        assert b.advance_to(4) == 2  # backwards advance refused

    def test_reset_clears_cursor_and_touched(self) -> None:
        brutex_feed = pytest.importorskip("brutex_feed", reason=_WHEEL_SKIP_REASON)
        b = brutex_feed.BarrierCore(True)
        assert b.advance_to(3) == 0
        assert b.access(3) == 0
        b.reset()
        assert b.current_index == -1
        assert b.touched() == []


class TestSeriesFfi:
    """The ``series_load`` decision arithmetic (needs the wheel)."""

    def test_ownership_cut_basic_and_abstain(self) -> None:
        brutex_feed = pytest.importorskip("brutex_feed", reason=_WHEEL_SKIP_REASON)
        d0 = 19_724  # (date(2024, 1, 2) - date(1970, 1, 1)).days
        bar_day1 = _SESSION_1_US
        bar_day2 = _SESSION_1_US + _DAY_US
        assert brutex_feed.ownership_cut([bar_day1, bar_day2], d0, d0) == [0]
        assert brutex_feed.ownership_cut([bar_day1, bar_day2], d0, d0 + 1) == [0, 1]
        assert brutex_feed.ownership_cut([], d0, d0) == []
        # Out-of-envelope timestamp → None (ABSTAIN — the caller raises).
        assert brutex_feed.ownership_cut([-1], d0, d0) is None

    def test_boundary_monotonic_break(self) -> None:
        brutex_feed = pytest.importorskip("brutex_feed", reason=_WHEEL_SKIP_REASON)
        assert brutex_feed.boundary_monotonic_break(5, 5) is True
        assert brutex_feed.boundary_monotonic_break(5, 6) is False

    def test_expected_bar_instant_smoke(self) -> None:
        brutex_feed = pytest.importorskip("brutex_feed", reason=_WHEEL_SKIP_REASON)
        midnight = 1_704_133_800_000_000  # 2024-01-02 00:00 IST in epoch µs
        open_us = midnight + (9 * 60 + 15) * _MINUTE_US
        close_us = midnight + (15 * 60 + 30) * _MINUTE_US
        sessions = [(midnight, open_us, close_us)]
        now = close_us + 10**12  # well past the session
        # A window containing the session-open label holds an instant.
        assert brutex_feed.expected_bar_instant(open_us, open_us, _MINUTE_US, now, sessions) is True
        # A degenerate window strictly between grid labels holds none.
        assert (
            brutex_feed.expected_bar_instant(open_us + 1, open_us + 2, _MINUTE_US, now, sessions)
            is False
        )

    def test_expected_bar_instant_refuses_out_of_envelope_magnitudes(self) -> None:
        """The FFI magnitude ceiling (``DEC-FFI-MAGNITUDE-CEILING-001``).

        ``series::expected_bar_instant``'s intraday label expression carries
        five unchecked ``i64`` ops, and ``rust/brutex-feed``'s
        ``[profile.release]`` sets BOTH ``overflow-checks = true`` and
        ``panic = "abort"`` — so an overflow there does not unwind into pyo3's
        catch-and-convert wrapper, it SIGABRTs THIS interpreter. The
        ``#[pyfunction]`` face therefore refuses every integer outside the
        crate's own fixed-IST envelope ``[0, MAX_SAFE_TS_US)`` — the same
        predicate ``ownership_cut`` abstains on — with a ``ValueError``
        naming the offending argument.

        Each argument position is driven, because the guard is one
        ``require_ts_envelope`` call per position and a dropped call would
        otherwise be invisible. The ``sessions`` triple is driven on
        ``open_us``: that is the component the label arithmetic subtracts, so
        it is the position that actually aborts.

        Production can never reach this: ``series_load.py`` derives every
        argument from a real ``datetime`` via ``_ts_epoch_us``.
        """
        brutex_feed = pytest.importorskip("brutex_feed", reason=_WHEEL_SKIP_REASON)
        midnight = 1_704_133_800_000_000
        open_us = midnight + (9 * 60 + 15) * _MINUTE_US
        close_us = midnight + (15 * 60 + 30) * _MINUTE_US
        ok_sessions = [(midnight, open_us, close_us)]
        # The envelope ceiling is a Rust const, deliberately NOT added to the
        # FFI handshake surface just to be readable here (that would grow
        # `_EXPECTED_COMPILED_SYMBOLS` + `EXPECTED_BUILD.json` + the `.pyi` for
        # a test's convenience). It is mirrored instead, with the source as the
        # drift pin — the `_TRUTHY`/`_FALSY` mirror-pin idiom this same file
        # already uses for `sweep_lookahead`'s vocabulary.
        max_safe = _MAX_SAFE_TS_US

        # A hand-derived tuple that REACHES the label expression: seg_start >
        # open (the else branch), a non-zero step, and an overlapping session.
        # With open_us = -(2**63) the very first op overflows.
        aborting_open = -(2**63)
        with pytest.raises(ValueError, match=r"sessions\[0\]\.open_us must be in \[0, "):
            brutex_feed.expected_bar_instant(1, 1, 1, 0, [(0, aborting_open, 2)])

        # Every other guarded position, each independently.
        for name, args in (
            ("seg_start_us", (-1, close_us, _MINUTE_US, close_us, ok_sessions)),
            ("seg_end_us", (open_us, max_safe, _MINUTE_US, close_us, ok_sessions)),
            ("step_us", (open_us, close_us, -1, close_us, ok_sessions)),
            ("now_us", (open_us, close_us, _MINUTE_US, -1, ok_sessions)),
            (
                "sessions[0].stamp_us",
                (open_us, close_us, _MINUTE_US, close_us, [(-1, open_us, close_us)]),
            ),
            (
                "sessions[0].close_us",
                (open_us, close_us, _MINUTE_US, close_us, [(midnight, open_us, max_safe)]),
            ),
        ):
            with pytest.raises(ValueError, match=rf"{re.escape(name)} must be in \[0, "):
                brutex_feed.expected_bar_instant(*args)

        # The boundary is exact and half-open: MAX_SAFE_TS_US - 1 is ACCEPTED,
        # so the guard refuses by overflow reachability, not by "large".
        assert (
            brutex_feed.expected_bar_instant(max_safe - 1, max_safe - 1, _MINUTE_US, 0, ok_sessions)
            is False
        )


class TestShimHandshake:
    """The wheel-source freshness handshake vs ``EXPECTED_BUILD.json``."""

    def test_shim_expected_symbols_match_expected_build_json(self) -> None:
        brutex_feed = pytest.importorskip("brutex_feed", reason=_WHEEL_SKIP_REASON)
        expected_build = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "rust/brutex-feed/python/brutex_feed/EXPECTED_BUILD.json"
            ).read_text(encoding="utf-8")
        )
        shim_symbols = brutex_feed._EXPECTED_COMPILED_SYMBOLS  # pyright: ignore[reportPrivateUsage]
        assert sorted(shim_symbols) == expected_build["symbols"]
        assert brutex_feed.__build_crate_version__ == expected_build["crate_version"]
