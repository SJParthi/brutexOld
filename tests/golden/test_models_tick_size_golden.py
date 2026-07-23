"""Frozen golden — the ``normalise_tick_size`` decision law as LITERAL outcomes.

Slice T35 PR-2 (ledger row pending QM mint): the ``BRUTEX_CORE_RUST_MODELS``
seam routes the tick-size decision law through ``rust/brutex-core``, so the
law is a cross-language contract. This golden pins the law the way the
DEC-MONEY-2DP-001 sibling (``tests/golden/test_money_rounding_golden.py``)
pins ``round_money``: as LITERAL expected outcomes — never oracle-vs-crate
parity alone (which would be vacuous if both drifted together).

THE ARBITER LAW (the fleet-obligation fold): every literal below was
captured from the EXECUTED live Python oracle
(``brutex/models.py::_normalise_tick_size_python``, derivation run
2026-07-17 under CPython's default decimal context) — NOT derived from an
assumed rounding-mode formula. The oracle arm (:class:`TestOracleArbiter`)
re-executes the live oracle against these literals on every run and NEVER
skips, so a drift in the Python behavior fails loudly naming the case; the
crate arm (:class:`TestCrateArm`) asserts the engaged seam against the SAME
literals and skips loudly only when the wheel is absent (the
core-rust-parity lane + local builds execute it).

Observed (not defined): on this corpus the oracle's 2dp ``quantize`` ties
behaved as banker's rounding under the default context — ``0.045`` → 0.04
(refused), ``0.105`` → ``0.10`` (served) — which DIVERGES from money's
HALF_UP law (that would give 0.05 / 0.11). The pinned literals are the
authority; the rounding-mode name is a description of what the oracle run
produced, not the contract.

A failure here means the LAW moved — a conscious DEC-level event, never a
refactor detail.
"""

from __future__ import annotations

import decimal
import sys
import types
from collections.abc import Callable, Iterator
from decimal import Decimal

import pytest
from brutex import models, models_rust

#: The frozen refuse-message tail for the validity-set refusals — a LITERAL
#: (captured from the executed oracle run), never computed from
#: ``VALID_TICK_SIZES_RUPEES`` (computing it here would un-pin the bytes).
_VALID_SET_TAIL = (
    " is not a known valid NSE/BSE tick size. Valid set: "
    "[Decimal('0.01'), Decimal('0.05'), Decimal('0.10'), Decimal('0.25'), "
    "Decimal('0.50'), Decimal('1.00'), Decimal('5.00')]"
)

#: (input, frozen expected outcome). Outcome kinds:
#:   ("value",  str(returned Decimal))            — byte-for-byte
#:   ("refuse", str(TickSizeFormatError message)) — byte-for-byte
#:   ("raise",  exception type name)              — a raw (non-refusal)
#:     decimal fault escaping the oracle; type-pinned (its message embeds
#:     context reprs, deliberately not frozen).
#: Every literal captured from the executed live oracle, 2026-07-17.
_TICK_GOLDEN: tuple[tuple[float | int | Decimal | str, tuple[str, str]], ...] = (
    # ── Decimals: the canonical valid set round-trips exponent-exact ──
    (Decimal("0.01"), ("value", "0.01")),
    (Decimal("0.05"), ("value", "0.05")),
    (Decimal("0.10"), ("value", "0.10")),
    (Decimal("0.25"), ("value", "0.25")),
    (Decimal("0.50"), ("value", "0.50")),
    (Decimal("1.00"), ("value", "1.00")),
    (Decimal("5.00"), ("value", "5.00")),
    # ── Decimals: exponent/precision shapes ──
    (Decimal("0.050"), ("value", "0.05")),
    (Decimal("1E-31"), ("refuse", "tick_size 0.00" + _VALID_SET_TAIL)),
    # ── Decimals: the rounding cells (ties + near-ties). The two tie
    # cells 0.045/0.105 are the arbiter's discriminating cells — the
    # observed oracle ties (0.04-refuse / 0.10-serve) diverge from
    # money's HALF_UP (which would read 0.05-serve / 0.11-refuse). ──
    (Decimal("0.049999"), ("value", "0.05")),
    (Decimal("0.0450001"), ("value", "0.05")),
    (Decimal("0.045"), ("refuse", "tick_size 0.04" + _VALID_SET_TAIL)),
    (Decimal("0.055"), ("refuse", "tick_size 0.06" + _VALID_SET_TAIL)),
    (Decimal("0.095"), ("value", "0.10")),
    (Decimal("0.105"), ("value", "0.10")),
    (Decimal("9.999"), ("refuse", "tick_size 10.00" + _VALID_SET_TAIL)),
    # ── Decimals: degenerates ──
    (Decimal("-0.001"), ("refuse", "tick_size -0.00" + _VALID_SET_TAIL)),
    (Decimal("0"), ("refuse", "tick_size 0.00" + _VALID_SET_TAIL)),
    (Decimal("-5"), ("refuse", "tick_size -5.00" + _VALID_SET_TAIL)),
    (Decimal("NaN"), ("refuse", "tick_size NaN" + _VALID_SET_TAIL)),
    (Decimal("Infinity"), ("raise", "InvalidOperation")),
    (Decimal("1E+30"), ("raise", "InvalidOperation")),
    # ── ints: the paise convention ──
    (1, ("value", "0.01")),
    (5, ("value", "0.05")),
    (10, ("value", "0.10")),
    (25, ("value", "0.25")),
    (50, ("value", "0.50")),
    (100, ("value", "1.00")),
    (500, ("value", "5.00")),
    (999, ("refuse", "tick_size 9.99" + _VALID_SET_TAIL)),
    (0, ("refuse", "tick_size integer out of expected paise range [1, 1000): 0")),
    (1000, ("refuse", "tick_size integer out of expected paise range [1, 1000): 1000")),
    (-5, ("refuse", "tick_size integer out of expected paise range [1, 1000): -5")),
    (True, ("refuse", "tick_size cannot be bool: True")),
    (False, ("refuse", "tick_size cannot be bool: False")),
    # ── floats: rupees vs integral-paise crossover ──
    (5.0, ("value", "0.05")),
    (10.0, ("value", "0.10")),
    (25.0, ("value", "0.25")),
    (100.0, ("value", "1.00")),
    (1.0, ("value", "1.00")),
    (0.1, ("value", "0.10")),
    (0.05, ("value", "0.05")),
    (0.045, ("refuse", "tick_size 0.04" + _VALID_SET_TAIL)),
    (0.105, ("value", "0.10")),
    (1.5, ("refuse", "tick_size 1.50" + _VALID_SET_TAIL)),
    (2.5, ("refuse", "tick_size 2.50" + _VALID_SET_TAIL)),
    (250.0, ("refuse", "tick_size 2.50" + _VALID_SET_TAIL)),
    (-5.0, ("refuse", "tick_size -5.00" + _VALID_SET_TAIL)),
    (0.07, ("refuse", "tick_size 0.07" + _VALID_SET_TAIL)),
    (1e16, ("refuse", "tick_size 100000000000000.00" + _VALID_SET_TAIL)),
    # float NaN raises RAW at the `candidate > 1` comparison (unlike
    # Decimal("NaN"), whose quiet quantize reaches the membership refuse).
    (float("nan"), ("raise", "InvalidOperation")),
    # ── strs: the rupees convention (never paise) ──
    ("0.05", ("value", "0.05")),
    ("0.10", ("value", "0.10")),
    ("1", ("value", "1.00")),
    ("5", ("value", "5.00")),
    ("0.07", ("refuse", "tick_size 0.07" + _VALID_SET_TAIL)),
    ("-0.05", ("refuse", "tick_size -0.05" + _VALID_SET_TAIL)),
    ("00.05", ("value", "0.05")),
    ("0.045", ("refuse", "tick_size 0.04" + _VALID_SET_TAIL)),
    ("0.105", ("value", "0.10")),
    (" 0.05", ("value", "0.05")),
    ("5e-2", ("value", "0.05")),
    ("junk", ("refuse", "Cannot parse tick_size 'junk'")),
    ("", ("refuse", "Cannot parse tick_size ''")),
)


def _outcome(
    fn: Callable[[float | int | Decimal | str], Decimal],
    raw: float | int | Decimal | str,
) -> tuple[str, str]:
    """Fold a call into the golden outcome shape (see :data:`_TICK_GOLDEN`)."""
    try:
        return ("value", str(fn(raw)))
    except models.TickSizeFormatError as exc:
        return ("refuse", str(exc))
    except decimal.DecimalException as exc:
        return ("raise", type(exc).__name__)


@pytest.fixture(autouse=True)
def _fresh_seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts flag-OFF with a fresh latch/burn-in/memo.

    Pinned to the explicit "0" kill switch (NOT delete-to-unset): under
    DEC-RUST-DEFAULT-ON-WAVE-001 an unset flag resolves default-ON, which
    would route the bomb-spy OFF arm to the crate handshake. "0" forces the
    genuine oracle so the flag-OFF-crate-untouched pin stays non-vacuous.
    """
    monkeypatch.setenv(models_rust.MODELS_RUST_ENV, "0")
    models_rust.reset_models_rust_for_tests()
    yield
    models_rust.reset_models_rust_for_tests()


class TestOracleArbiter:
    """The live-Python-oracle arm — the ARBITER. NEVER skips."""

    @pytest.mark.parametrize(("raw", "expected"), _TICK_GOLDEN)
    def test_oracle_matches_pinned_literal(
        self, raw: float | int | Decimal | str, expected: tuple[str, str]
    ) -> None:
        actual = _outcome(
            models._normalise_tick_size_python,  # pyright: ignore[reportPrivateUsage]
            raw,
        )
        assert actual == expected, f"live Python oracle drifted from the golden at {raw!r}"

    @pytest.mark.parametrize(("raw", "expected"), _TICK_GOLDEN)
    def test_public_flag_off_matches_pinned_literal_crate_untouched(
        self,
        monkeypatch: pytest.MonkeyPatch,
        raw: float | int | Decimal | str,
        expected: tuple[str, str],
    ) -> None:
        """Flag-OFF (the default) the public law serves the SAME literals
        with the crate provably untouched (bomb spy)."""
        bomb = types.ModuleType("brutex_core")

        def _boom(_kind: str, _payload: str) -> str:
            raise AssertionError("the crate must not be called flag-OFF")

        bomb.models_normalise_tick_size_str = _boom  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "brutex_core", bomb)
        assert _outcome(models.normalise_tick_size, raw) == expected

    def test_observed_tie_cells_are_the_executed_oracle_not_half_up(self) -> None:
        """The discriminating cells, restated: the ORACLE RUN produced
        0.04-refuse / ``0.10``-serve on the .5 tie cells (money's HALF_UP
        law would produce 0.05-serve / 0.11-refuse). Observation from the
        executed oracle — the literals above are the contract."""
        with pytest.raises(models.TickSizeFormatError, match="tick_size 0.04"):
            models._normalise_tick_size_python(Decimal("0.045"))  # pyright: ignore[reportPrivateUsage]
        assert str(models._normalise_tick_size_python(Decimal("0.105"))) == "0.10"  # pyright: ignore[reportPrivateUsage]
        # money's HALF_UP on the same cells — the cross-law divergence pin.
        from brutex.money import round_money

        assert str(round_money(Decimal("0.045"))) == "0.05"
        assert str(round_money(Decimal("0.105"))) == "0.11"


class TestCrateArm:
    """Flag-ON against the real wheel — the SAME literals. Skips loudly
    (importorskip) when the wheel is absent; the oracle arm above never
    does."""

    @pytest.fixture(autouse=True)
    def _require_wheel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        crate = pytest.importorskip(
            "brutex_core", reason="brutex-core wheel not built in this env (parity lane covers it)"
        )
        if not callable(getattr(crate, "models_normalise_tick_size_str", None)):
            pytest.skip("installed brutex-core wheel predates the models seam")
        monkeypatch.setenv(models_rust.MODELS_RUST_ENV, "1")

    @pytest.mark.parametrize(("raw", "expected"), _TICK_GOLDEN)
    def test_engaged_seam_matches_pinned_literal(
        self, raw: float | int | Decimal | str, expected: tuple[str, str]
    ) -> None:
        models_rust.reset_models_rust_for_tests(remaining=0)  # steady state — no oracle cushion
        assert _outcome(models.normalise_tick_size, raw) == expected, (
            f"engaged crate seam drifted from the golden at {raw!r}"
        )
        assert not models_rust.models_rust_disabled(), (
            f"seam silently latched OFF at {raw!r} — the crate leg would be vacuous"
        )
