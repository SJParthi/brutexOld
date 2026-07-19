"""Hostile dual-arm differential for the ``combo_rust`` facade vs the REAL wheel.

DEC-STRATEGIES-RUST-COMBO-001 (§4.2). Requires the ``brutex_strategies``
wheel (skipped honestly when absent — the CI unit-test job installs it, so
the differential EXECUTES there, never skip-only). Every routed value is
compared byte-for-byte against the verbatim Python oracle:

* the hypothesis canonical-JSON property over the LOWERED-tree domain
  (str/int/bool/None/list/dict — the only shapes ``_canonicalise_value``
  can emit) with adversarial keys + strings;
* the enumerated hostile corpus — astral/non-BMP sort adversaries, embedded
  ``\\x1f``, empty shapes, nested list-of-dict, the i128 boundary law
  (``i128::MIN``/``MAX`` ROUTE; ``2**127`` / ``−2**127−1`` / ``±2**200``
  DEGRADE with the warn-once event), the ``Weird(int)``
  ``__index__``-diverging TRUE-VALUE pin, str-subclass keys (REFUSE),
  lone-surrogate keys AND values (DEGRADE) — each degrade/refusal case
  asserts BYTE-EQUAL output vs the oracle AND the expected event;
* the scan-offender differential (the ONLY teeth on the scan law's
  detect-half — the runtime burn-in never sees an offender; an explicit
  flip-DEC precondition) + the scan boundary matrix + the three REAL call
  sites driven flag-ON with message-bytes equality vs flag-OFF;
* the frozen cross-language canonical-params golden (the pytest half of the
  cargo-pinned literal);
* the end-to-end ``canonicalise_params`` differential with RAW params
  (floats / Decimals / -0.0 / tuples / ranges — lowering precedes the seam).
"""

from __future__ import annotations

import json
from typing import Any

import brutex.strategies.combo_rust as cr
import pytest
from brutex.strategies.combo import (
    _first_offending_field,
    _stdlib_canonical_json,
    canonicalise_params,
    combo_hash,
)
from brutex.strategies.errors import StrategyError
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

pytest.importorskip(
    "brutex_strategies",
    reason="brutex_strategies wheel not installed — the dual-arm differential needs the kernel",
)

from decimal import Decimal


def _rust_on(mp: pytest.MonkeyPatch) -> None:
    """Flag ON + post-burn-in steady state (Rust bytes ship, no oracle cushion)."""
    mp.setenv(cr.STRATEGIES_RUST_COMBO_ENV, "1")
    cr.reset_combo_rust_for_tests(remaining=0)


@pytest.fixture(autouse=True)
def _facade_reset() -> Any:
    """Fresh facade state around every test (latch/burn-in/memo isolation)."""
    cr.reset_combo_rust_for_tests()
    yield
    cr.reset_combo_rust_for_tests()


def _dumps(tree: Any) -> str:
    """The independent stdlib oracle expression (recomputed, never imported)."""
    return json.dumps(tree, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


#: Adversarial dict keys — empty, the code-point sort ladder incl. the
#: non-BMP boundary, escapes-needing keys.
_KEY_CORPUS = [
    "",
    "Z",
    "a",
    "é",
    "￿",
    "\U00010000",
    "sl_paisa",
    'q"uote',
    "back\\slash",
    "ctl\x01",
    "\x7f",
    "\x1f",
]

#: Adversarial string values — quotes/backslashes/C0 controls/0x7f/astral/
#: U+2028/U+2029 + the embedded field-join delimiter.
_VALUE_CORPUS = [
    "",
    "é",
    "₹",
    "😀",
    "  ",
    "a/b",
    'x"y\\z',
    "ctl\x00\x1f\x7f",
    "\U0001f600mix\x08\x0c",
]

_KEYS = st.one_of(st.sampled_from(_KEY_CORPUS), st.text(max_size=8))
_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**127), max_value=2**127 - 1),
    st.sampled_from(_VALUE_CORPUS),
    st.text(max_size=12),
)
_TREES = st.recursive(
    _SCALARS,
    lambda inner: st.one_of(
        st.lists(inner, max_size=4),
        st.dictionaries(_KEYS, inner, max_size=4),
    ),
    max_leaves=24,
)


class TestCanonicalDifferential:
    @settings(max_examples=200, deadline=None)
    @given(tree=st.dictionaries(_KEYS, _TREES, max_size=5))
    def test_lowered_tree_domain(self, tree: dict[str, Any]) -> None:
        with pytest.MonkeyPatch.context() as mp:
            _rust_on(mp)
            routed = cr.routed_canonical_json(tree, python_law=_stdlib_canonical_json)
        assert routed == _dumps(tree)

    def test_sort_adversaries_route_without_degrade(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _rust_on(monkeypatch)
        tree = {key: i for i, key in enumerate(_KEY_CORPUS)}
        with capture_logs() as logs:
            routed = cr.routed_canonical_json(tree, python_law=_stdlib_canonical_json)
        assert routed == _dumps(tree)
        assert [e for e in logs if e["event"] == "combo_rust.fallback"] == []
        assert cr.rust_combo_disabled() is False

    def test_hostile_value_corpus_and_shapes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _rust_on(monkeypatch)
        tree: dict[str, Any] = {
            "values": list(_VALUE_CORPUS),
            "empty_dict": {},
            "empty_list": [],
            "nested": [{"inner": [{"deep": "\U00010000"}]}, {"z": None}],
            "bools": [None, True, False, 1, 0],
        }
        with capture_logs() as logs:
            routed = cr.routed_canonical_json(tree, python_law=_stdlib_canonical_json)
        assert routed == _dumps(tree)
        assert [e for e in logs if e["event"] == "combo_rust.fallback"] == []

    def test_i128_boundaries_route_without_degrade(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The reconciled range law: EXACTLY extract::<i128>()'s native range
        # routes — i128::MIN = −2**127 INCLUDED (no separate magnitude gate).
        _rust_on(monkeypatch)
        tree = {"lo": -(2**127), "hi": 2**127 - 1, "u64max": 2**64 - 1}
        with capture_logs() as logs:
            routed = cr.routed_canonical_json(tree, python_law=_stdlib_canonical_json)
        assert routed == _dumps(tree)
        assert [e for e in logs if e["event"] == "combo_rust.fallback"] == []
        assert cr.rust_combo_disabled() is False

    @pytest.mark.parametrize(
        "big",
        [2**127, -(2**127) - 1, 2**200, -(2**200)],
        ids=["two_pow_127", "minus_two_pow_127_minus_1", "two_pow_200", "minus_two_pow_200"],
    )
    def test_beyond_i128_degrades_byte_equal_with_warn(
        self, monkeypatch: pytest.MonkeyPatch, big: int
    ) -> None:
        _rust_on(monkeypatch)
        tree = {"big": big}
        with capture_logs() as logs:
            routed = cr.routed_canonical_json(tree, python_law=_stdlib_canonical_json)
        assert routed == _dumps(tree)  # the oracle serves, byte-equal
        warns = [e for e in logs if e["event"] == "combo_rust.fallback"]
        assert warns[0]["reason"] == "canonical_json_error"
        assert cr.rust_combo_disabled() is False  # degrade, never latch

    def test_weird_index_int_subclass_renders_true_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # R2-B-NIT-1: extraction reads the TRUE PyLong value (the C-level
        # PyNumber_Index PyLong short-circuit), never the Python-level
        # __index__/__int__/__repr__ overrides.
        class Weird(int):
            def __index__(self) -> int:
                return 999

            def __int__(self) -> int:
                return 888

            def __repr__(self) -> str:
                return "777"

        _rust_on(monkeypatch)
        tree = {"w": Weird(42)}
        with capture_logs() as logs:
            routed = cr.routed_canonical_json(tree, python_law=_stdlib_canonical_json)
        assert routed == '{"w":42}'
        assert routed == _dumps(tree)  # CPython json also renders 42
        assert [e for e in logs if e["event"] == "combo_rust.fallback"] == []

    def test_lone_surrogate_value_degrades_byte_equal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A lone surrogate CANNOT cross PyO3 into a Rust String — extraction
        # raises UnicodeEncodeError ⊂ ValueError → degrade; the oracle
        # escapes it \udXXX. Parity holds via degrade.
        _rust_on(monkeypatch)
        tree = {"k": "\ud800tail"}
        with capture_logs() as logs:
            routed = cr.routed_canonical_json(tree, python_law=_stdlib_canonical_json)
        assert routed == _dumps(tree)
        assert "\\ud800" in routed
        warns = [e for e in logs if e["event"] == "combo_rust.fallback"]
        assert warns[0]["reason"] == "canonical_json_error"

    def test_str_subclass_key_refused_byte_equal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The crate's exact-str KEY gate: a subclass key could sort
        # differently under CPython's sort_keys — REFUSED with the
        # "non-exact str" marker → the distinct nonexact_key_refused reason.
        class K(str):
            __slots__ = ()

        _rust_on(monkeypatch)
        tree = {K("a"): 1, "b": 2}
        with capture_logs() as logs:
            routed = cr.routed_canonical_json(tree, python_law=_stdlib_canonical_json)
        assert routed == _dumps(tree)
        warns = [e for e in logs if e["event"] == "combo_rust.fallback"]
        assert warns[0]["reason"] == "nonexact_key_refused"
        assert cr.rust_combo_disabled() is False  # refuse, never latch

    def test_lone_surrogate_exact_str_key_degrades_byte_equal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # R1-MINOR-1: an EXACT-str key carrying a lone surrogate passes the
        # exact-type gate and fails one step later at String extraction —
        # the second fail-closed key path (degrade, not refuse).
        _rust_on(monkeypatch)
        tree = {"\udc80": 1}
        with capture_logs() as logs:
            routed = cr.routed_canonical_json(tree, python_law=_stdlib_canonical_json)
        assert routed == _dumps(tree)
        warns = [e for e in logs if e["event"] == "combo_rust.fallback"]
        assert warns[0]["reason"] == "canonical_json_error"

    def test_bare_float_direct_call_degrades_byte_equal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Unreachable from canonicalise_params (lowering rejects/converts
        # floats first); a hostile DIRECT facade call degrades to the
        # oracle, which serializes exactly as stdlib json would.
        _rust_on(monkeypatch)
        tree = {"f": 1.5}
        with capture_logs() as logs:
            routed = cr.routed_canonical_json(tree, python_law=_stdlib_canonical_json)
        assert routed == _dumps(tree)
        warns = [e for e in logs if e["event"] == "combo_rust.fallback"]
        assert warns[0]["reason"] == "canonical_json_error"

    def test_end_to_end_canonicalise_params_raw_params(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # RAW params through the REAL public function — lowering precedes
        # the seam (floats→repr strings, Decimal→str, -0.0 normalised,
        # tuple/range→arrays), so parity holds structurally; this pins the
        # seam PLACEMENT.
        raw: dict[str, Any] = {
            "f": 1.5,
            "neg_zero": -0.0,
            "exp": 1e300,
            "dec": Decimal("1.00"),
            "tup": (1, 2, 3),
            "rng": range(3),
            "nested": {"z": [Decimal("2.5"), 0.1]},
        }
        monkeypatch.setenv(cr.STRATEGIES_RUST_COMBO_ENV, "0")
        cr.reset_combo_rust_for_tests()
        off_arm = canonicalise_params(raw)
        _rust_on(monkeypatch)
        on_arm = canonicalise_params(raw)
        assert on_arm == off_arm
        assert '"neg_zero":"0.0"' in on_arm  # the -0.0 → +0.0 → repr law survived
        assert '"dec":"1.00"' in on_arm  # Decimal scale is significant

    def test_frozen_cross_language_golden(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The pytest half of the cargo-pinned literal (I1's frozen golden) —
        # the cross-language byte oracle, BOTH arms.
        params: dict[str, Any] = {
            "sl_paisa": 2000,
            "tp_paisa": None,
            "prior_n": 20,
            "proximity_bps": [10, 15, 25],
            "fib_anchor": "prev_day",
            "start_equity_paisa": 10000000,
            "fill_mode": "worst_case",
        }
        frozen = (
            '{"fib_anchor":"prev_day","fill_mode":"worst_case","prior_n":20,'
            '"proximity_bps":[10,15,25],"sl_paisa":2000,'
            '"start_equity_paisa":10000000,"tp_paisa":null}'
        )
        monkeypatch.setenv(cr.STRATEGIES_RUST_COMBO_ENV, "0")
        cr.reset_combo_rust_for_tests()
        assert canonicalise_params(params) == frozen
        _rust_on(monkeypatch)
        with capture_logs() as logs:
            assert canonicalise_params(params) == frozen
        assert [e for e in logs if e["event"] == "combo_rust.fallback"] == []
        assert cr.rust_combo_disabled() is False

    def test_engaged_event_proves_the_wheel_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Anti-vacuous: the differential is meaningless if the Rust path
        # never engaged — pin the engaged INFO + the live (unlatched) state.
        _rust_on(monkeypatch)
        with capture_logs() as logs:
            cr.routed_canonical_json({"a": 1}, python_law=_stdlib_canonical_json)
        engaged = [e for e in logs if e["event"] == "combo_rust.engaged"]
        assert len(engaged) == 1
        assert cr.rust_combo_disabled() is False


#: Scan-value alphabet with a high offender frequency (the \x1f member).
_SCAN_VALUES = st.text(alphabet=st.sampled_from(list("ab\x1fé😀 ")), max_size=6)


class TestScanDifferential:
    @settings(max_examples=200, deadline=None)
    @given(
        pairs=st.lists(
            st.tuples(st.text(max_size=6), st.one_of(_SCAN_VALUES, st.text(max_size=8))),
            max_size=6,
        )
    )
    def test_offender_name_parity(self, pairs: list[tuple[str, str]]) -> None:
        # The detect-half differential — the ONLY teeth on offender
        # DETECTION (the runtime burn-in provably never sees an offender);
        # an explicit precondition of any future default-ON flip DEC.
        with pytest.MonkeyPatch.context() as mp:
            _rust_on(mp)
            routed = cr.routed_first_offending_field(pairs, python_law=_first_offending_field)
        assert routed == _first_offending_field(pairs)

    @pytest.mark.parametrize(
        "fields",
        [
            (),
            (("a", ""),),
            (("a", "\x1f"),),
            (("a", "\x1fstart"),),
            (("a", "mid\x1fdle"),),
            (("a", "end\x1f"),),
            (("a", "é\x1f😀"),),  # multi-byte neighbors
            (("a", "clean"), ("b", "also clean")),
            (("first", "\x1f"), ("second", "\x1f")),
            (("a", "clean"), ("b", "x\x1fy"), ("c", "\x1f")),
        ],
        ids=[
            "empty",
            "empty_value",
            "sole_sep",
            "sep_at_start",
            "sep_in_middle",
            "sep_at_end",
            "multibyte_neighbors",
            "all_clean",
            "offender_at_index_zero",
            "first_of_several",
        ],
    )
    def test_scan_boundary_matrix(
        self, monkeypatch: pytest.MonkeyPatch, fields: tuple[tuple[str, str], ...]
    ) -> None:
        _rust_on(monkeypatch)
        routed = cr.routed_first_offending_field(fields, python_law=_first_offending_field)
        assert routed == _first_offending_field(fields)

    def test_combo_hash_offender_message_bytes_on_equals_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kwargs: dict[str, Any] = {
            "strategy_id": "mask:1f",
            "direction": "bull",
            "instrument": "NSE-NIFTY",
            "tf": "5minute",
            "mode": "A\x1fB",  # the offender
            "params": {"a": 1},
            "data_snapshot_hash": "d",
            "indicator_version": "v",
            "code_commit_sha": "c",
        }
        monkeypatch.setenv(cr.STRATEGIES_RUST_COMBO_ENV, "0")
        cr.reset_combo_rust_for_tests()
        with pytest.raises(StrategyError) as off_exc:
            combo_hash(**kwargs)
        _rust_on(monkeypatch)
        with pytest.raises(StrategyError) as on_exc:
            combo_hash(**kwargs)
        assert str(on_exc.value) == str(off_exc.value)
        assert str(on_exc.value) == (
            "combo_hash: mode must not contain the \\x1f "
            "unit-separator byte (it is the field join delimiter)"
        )

    def test_combo_hash_clean_digest_on_equals_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        kwargs: dict[str, Any] = {
            "strategy_id": "mask:1f",
            "direction": "bull",
            "instrument": "NSE-NIFTY",
            "tf": "5minute",
            "mode": "A",
            "params": {"sl_paisa": 2000, "prior_n": 20},
            "data_snapshot_hash": "d" * 64,
            "indicator_version": "trading-core-0.3.2",
            "code_commit_sha": "c" * 40,
        }
        monkeypatch.setenv(cr.STRATEGIES_RUST_COMBO_ENV, "0")
        cr.reset_combo_rust_for_tests()
        off_digest = combo_hash(**kwargs)
        _rust_on(monkeypatch)
        assert combo_hash(**kwargs) == off_digest

    def test_refinement_run_id_mirror_message_bytes_on_equals_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The engine mirror routes through the SAME common guard — the seam
        # lives inside reject_unit_separator, so the mirror is covered
        # automatically (design §1.1).
        stage3_refine = pytest.importorskip(
            "brutex.engine.stage3_refine",
            reason="engine deps not installed — the mirror call-site leg needs them",
        )
        budget = stage3_refine.RefineBudget()
        kwargs: dict[str, Any] = {
            "mask_run_id": "x\x1fy",
            "budget": budget,
            "seed": 7,
            "pycma_version": "3.3.0",
            "numpy_version": "2.0.0",
        }
        monkeypatch.setenv(cr.STRATEGIES_RUST_COMBO_ENV, "0")
        cr.reset_combo_rust_for_tests()
        with pytest.raises(stage3_refine.Stage3RefineError) as off_exc:
            stage3_refine.refinement_run_id(**kwargs)
        _rust_on(monkeypatch)
        with pytest.raises(stage3_refine.Stage3RefineError) as on_exc:
            stage3_refine.refinement_run_id(**kwargs)
        assert str(on_exc.value) == str(off_exc.value)
        # Clean-path digest parity through the same mirror.
        kwargs["mask_run_id"] = "a" * 64
        monkeypatch.setenv(cr.STRATEGIES_RUST_COMBO_ENV, "0")
        cr.reset_combo_rust_for_tests()
        off_id = stage3_refine.refinement_run_id(**kwargs)
        _rust_on(monkeypatch)
        assert stage3_refine.refinement_run_id(**kwargs) == off_id

    def test_walk_forward_run_hash_mirror_message_bytes_on_equals_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sweep_driver = pytest.importorskip(
            "brutex.engine.sweep_driver",
            reason="engine deps not installed — the mirror call-site leg needs them",
        )
        from brutex.strategies.types import Side

        def _call(schedule_hash: str) -> str:
            digest: str = sweep_driver._walk_forward_run_hash(
                schedule_hash,
                required_mask=11,
                direction=Side.BUY,
                tf_minutes=5,
                stop_loss_paisa=None,
                take_profit_paisa=None,
                start_equity_paisa=10_000_000,
                proximity=None,
                prior_n=3,
            )
            return digest

        monkeypatch.setenv(cr.STRATEGIES_RUST_COMBO_ENV, "0")
        cr.reset_combo_rust_for_tests()
        with pytest.raises(ValueError, match="schedule_hash") as off_exc:
            _call("x\x1fy")
        _rust_on(monkeypatch)
        with pytest.raises(ValueError, match="schedule_hash") as on_exc:
            _call("x\x1fy")
        assert str(on_exc.value) == str(off_exc.value)
        # Clean-path digest parity through the same mirror.
        monkeypatch.setenv(cr.STRATEGIES_RUST_COMBO_ENV, "0")
        cr.reset_combo_rust_for_tests()
        off_hash = _call("s" * 64)
        _rust_on(monkeypatch)
        assert _call("s" * 64) == off_hash
