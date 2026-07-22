"""Tests for :mod:`brutex.config.dotenv_rust` — the default-OFF dotenv seam.

DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943, slice T35 milestone 3).
Wheel-free fake-crate coverage of every facade layer + the mandated
hypothesis LINE-PARSE differential vs the live oracle in
:class:`TestRealCrateParity` (skips cleanly when the wheel is absent;
the ``core-rust-parity.yml`` lane runs it for real). Secret-safety: this
seam redacts values — pinned here at the facade level (the mechanism pin
lives in ``test_config_seam_core.py``).
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path

import pytest
from brutex.config import dotenv, dotenv_rust, seam_core

# ── Harness ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(dotenv_rust.DOTENV_RUST_ENV, raising=False)
    dotenv_rust.reset_dotenv_rust_for_tests()
    yield
    dotenv_rust.reset_dotenv_rust_for_tests()


def _oracle_pairs(text: str) -> list[tuple[str, str]]:
    return list(dotenv._parse_dotenv_python(text).items())  # pyright: ignore[reportPrivateUsage]


def _fake_crate(**overrides: object) -> types.ModuleType:
    crate = types.ModuleType("brutex_core")
    crate.__version__ = "0.0-fake"  # type: ignore[attr-defined]
    defaults: dict[str, object] = {"dotenv_parse_pairs": _oracle_pairs}
    defaults.update(overrides)
    for name, fn in defaults.items():
        setattr(crate, name, fn)
    return crate


def _bomb_crate() -> types.ModuleType:
    def _boom(*_args: object) -> list[tuple[str, str]]:
        raise AssertionError("the crate must not be called on this path")

    return _fake_crate(dotenv_parse_pairs=_boom)


def _arm(monkeypatch: pytest.MonkeyPatch, crate: types.ModuleType | None) -> None:
    monkeypatch.setenv(dotenv_rust.DOTENV_RUST_ENV, "1")
    monkeypatch.setitem(sys.modules, "brutex_core", crate)


_SAMPLE = "# c\nA=1\nexport B = 'two' \nC=a=b\n"
_SAMPLE_PARSED = {"A": "1", "B": "two", "C": "a=b"}


# ── Flag + constants ────────────────────────────────────────────────────────


class TestFlagAndConstants:
    def test_env_name_pinned_and_default_off(self) -> None:
        assert dotenv_rust.DOTENV_RUST_ENV == "BRUTEX_CORE_RUST_DOTENV"
        assert dotenv_rust.dotenv_rust_enabled() is False
        assert dotenv_rust.dotenv_rust_disabled() is False

    def test_flag_delegates_to_the_shared_tristate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(dotenv_rust.DOTENV_RUST_ENV, "true")
        assert dotenv_rust.dotenv_rust_enabled() is True
        monkeypatch.setenv(dotenv_rust.DOTENV_RUST_ENV, "no")
        assert dotenv_rust.dotenv_rust_enabled() is False

    def test_seam_is_value_redacting(self) -> None:
        # The dotenv secret-safety law: this seam NEVER logs values.
        assert dotenv_rust._SEAM.redact_values is True  # pyright: ignore[reportPrivateUsage]

    def test_reset_seam_restores_budget(self) -> None:
        dotenv_rust.reset_dotenv_rust_for_tests(remaining=0)
        assert dotenv_rust._SEAM.burn_in_remaining("parse") == 0  # pyright: ignore[reportPrivateUsage]
        dotenv_rust.reset_dotenv_rust_for_tests()
        assert (
            dotenv_rust._SEAM.burn_in_remaining("parse")  # pyright: ignore[reportPrivateUsage]
            == seam_core.BURN_IN_SAMPLES
        )


# ── OFF arm ─────────────────────────────────────────────────────────────────


class TestFlagOffNeverTouchesCrate:
    def test_off_arm_serves_oracle_and_never_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "brutex_core", _bomb_crate())

        def _resolver_bomb() -> types.ModuleType | None:
            raise AssertionError("_resolve_crate must not run flag-OFF")

        monkeypatch.setattr(
            dotenv_rust._SEAM,  # pyright: ignore[reportPrivateUsage]
            "_resolve_crate",
            _resolver_bomb,
        )
        assert dotenv.parse_dotenv(_SAMPLE) == _SAMPLE_PARSED
        assert dotenv_rust.dotenv_rust_disabled() is False

    def test_explicit_falsy_arm_is_off_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(dotenv_rust.DOTENV_RUST_ENV, "false")
        monkeypatch.setitem(sys.modules, "brutex_core", _bomb_crate())
        assert dotenv.parse_dotenv("A=1") == {"A": "1"}
        assert dotenv_rust.dotenv_rust_disabled() is False


# ── Eligibility ─────────────────────────────────────────────────────────────


class TestEligibility:
    def test_non_ascii_text_routes_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, _bomb_crate())
        assert dotenv.parse_dotenv("A=café") == {"A": "café"}
        assert dotenv_rust.dotenv_rust_disabled() is False

    def test_oversize_text_routes_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, _bomb_crate())
        big = "A=" + "x" * (1 << 20)
        assert dotenv.parse_dotenv(big) == {"A": "x" * (1 << 20)}
        assert dotenv_rust.dotenv_rust_disabled() is False

    def test_eligibility_predicate_direct(self) -> None:
        eligible = dotenv_rust._eligible_text  # pyright: ignore[reportPrivateUsage]
        assert eligible("A=1") is True
        assert eligible(b"A=1") is False
        assert eligible("é") is False
        assert eligible("x" * ((1 << 20) + 1)) is False


# ── Refuse-poison ───────────────────────────────────────────────────────────


class TestRefusePoison:
    @pytest.mark.parametrize(
        "poison",
        [
            42,
            ("A", "1"),  # not a list
            [["A", "1"]],  # list-not-tuple item
            [("A",)],  # wrong arity
            [(1, "v")],  # non-str key
            [("A", 2)],  # non-str value
            [("", "v")],  # empty key
            [("é", "v")],  # non-ascii key
            [("A", "é")],  # non-ascii value
        ],
    )
    def test_poison_pairs_refused_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        _arm(monkeypatch, _fake_crate(dotenv_parse_pairs=lambda _t: poison))
        assert dotenv.parse_dotenv(_SAMPLE) == _SAMPLE_PARSED
        assert dotenv_rust.dotenv_rust_disabled() is False

    def test_poison_warn_is_value_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        records: list[dict[str, object]] = []
        monkeypatch.setattr(
            seam_core._LOG,  # pyright: ignore[reportPrivateUsage]
            "warning",
            lambda event, **kw: records.append({"event": event, **kw}),
        )
        _arm(
            monkeypatch,
            _fake_crate(dotenv_parse_pairs=lambda _t: [("KEY", "SECRET-VALUE-abc")]),
        )
        # A non-ascii-free poison shape: empty-key entry carrying a value.
        _arm(monkeypatch, _fake_crate(dotenv_parse_pairs=lambda _t: [("", "SECRET-VALUE-abc")]))
        assert dotenv.parse_dotenv("A=1") == {"A": "1"}
        assert records, "the refusal must warn"
        assert all(
            "SECRET-VALUE-abc" not in str(v) for record in records for v in record.values()
        ), "a parsed value leaked into a log record (the redact law)"


# ── Burn-in + degrade + wiring ──────────────────────────────────────────────


class TestBurnInAndDegrade:
    def test_burn_in_mismatch_autoreverts_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        records: list[dict[str, object]] = []
        monkeypatch.setattr(
            seam_core._LOG,  # pyright: ignore[reportPrivateUsage]
            "error",
            lambda event, **kw: records.append({"event": event, **kw}),
        )
        _arm(
            monkeypatch,
            _fake_crate(dotenv_parse_pairs=lambda _t: [("WRONG", "SECRET-WRONG-VAL")]),
        )
        assert dotenv.parse_dotenv(_SAMPLE) == _SAMPLE_PARSED  # oracle shipped
        assert dotenv_rust.dotenv_rust_disabled() is True
        assert records[0]["event"] == "dotenv_rust.autoreverted"
        assert all("SECRET-WRONG-VAL" not in str(v) for v in records[0].values()), (
            "a mismatch value leaked into the autorevert record (the redact law)"
        )

    def test_order_divergence_is_a_burn_in_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # dict == ignores order; the facade compares list(items()) —
        # order-STRICT (the first-insertion-position law has teeth).
        _arm(
            monkeypatch,
            _fake_crate(dotenv_parse_pairs=lambda t: list(reversed(_oracle_pairs(t)))),
        )
        assert dotenv.parse_dotenv("A=1\nB=2") == {"A": "1", "B": "2"}
        assert dotenv_rust.dotenv_rust_disabled() is True

    def test_crate_error_degrades_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_t: str) -> list[tuple[str, str]]:
            raise ValueError("crate refusal")

        _arm(monkeypatch, _fake_crate(dotenv_parse_pairs=_boom))
        assert dotenv.parse_dotenv(_SAMPLE) == _SAMPLE_PARSED
        assert dotenv_rust.dotenv_rust_disabled() is False

    def test_routed_wiring_crosses_the_raw_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []

        def _spy(text: str) -> list[tuple[str, str]]:
            seen.append(text)
            return _oracle_pairs(text)

        _arm(monkeypatch, _fake_crate(dotenv_parse_pairs=_spy))
        dotenv_rust.reset_dotenv_rust_for_tests(remaining=0)
        assert dotenv.parse_dotenv(_SAMPLE) == _SAMPLE_PARSED
        assert seen == [_SAMPLE]

    def test_load_dotenv_file_rides_the_seam(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / ".env"
        target.write_text("A=1\n", encoding="utf-8")
        _arm(monkeypatch, _fake_crate())
        dotenv_rust.reset_dotenv_rust_for_tests(remaining=0)
        assert dotenv.load_dotenv_file(target) == {"A": "1"}


# ── ON-arm parity with the REAL wheel (skips cleanly when absent) ───────────


class TestRealCrateParity:
    @pytest.fixture(autouse=True)
    def _require_wheel(self) -> None:
        pytest.importorskip(
            "brutex_core", reason="brutex-core wheel not built in this env (parity lane covers it)"
        )

    def test_corpus_identical_both_arms_with_engagement_spy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex_core

        corpus = [
            "",
            "\n",
            _SAMPLE,
            "A=1\r\nB=2\rC=3\x0bD=4",
            "export A='x'\nexport export B=2",
            "A=1\nB=2\nA=3",
            "\x1fA\x1f = \x1f1\x1f",
            "A=''x''\n#c\nnokey\n=nokey",
            "K=" + "v" * 200,
        ]
        off = [dotenv.parse_dotenv(t) for t in corpus]

        calls: list[str] = []
        real = brutex_core.dotenv_parse_pairs

        def _spy(text: str) -> list[tuple[str, str]]:
            calls.append(text)
            return real(text)

        monkeypatch.setattr(brutex_core, "dotenv_parse_pairs", _spy)
        monkeypatch.setenv(dotenv_rust.DOTENV_RUST_ENV, "1")
        dotenv_rust.reset_dotenv_rust_for_tests(remaining=0)  # steady state
        on = [dotenv.parse_dotenv(t) for t in corpus]
        assert on == off
        assert [list(d.items()) for d in on] == [list(d.items()) for d in off]  # order-strict
        assert len(calls) == len(corpus)  # genuinely ENGAGED
        assert dotenv_rust.dotenv_rust_disabled() is False

    def test_real_crate_burn_in_completes_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(dotenv_rust.DOTENV_RUST_ENV, "1")
        dotenv_rust.reset_dotenv_rust_for_tests()  # full budget live
        for _ in range(seam_core.BURN_IN_SAMPLES + 1):
            assert dotenv.parse_dotenv(_SAMPLE) == _SAMPLE_PARSED
        assert dotenv_rust.dotenv_rust_disabled() is False
        assert dotenv_rust._SEAM.burn_in_remaining("parse") == 0  # pyright: ignore[reportPrivateUsage]

    def test_hypothesis_differential_line_parse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The MANDATED line-parse differential: random ASCII text incl.
        # every CPython strip/splitlines special, both arms equal.
        from hypothesis import HealthCheck, given, settings
        from hypothesis import strategies as st

        monkeypatch.setenv(dotenv_rust.DOTENV_RUST_ENV, "1")

        alphabet = st.sampled_from(
            [
                *"AB=#'\" xyz19_-",
                *["\t", "\n", "\r", "\r\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x1f"],
                *["export ", "export", "=", "=="],
            ]
        )
        texts = st.builds("".join, st.lists(alphabet, min_size=0, max_size=40))

        @settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
        @given(text=texts)
        def _differential(text: str) -> None:
            dotenv_rust.reset_dotenv_rust_for_tests(remaining=0)
            off = dotenv._parse_dotenv_python(text)  # pyright: ignore[reportPrivateUsage]
            on = dotenv.parse_dotenv(text)
            assert on == off
            assert list(on.items()) == list(off.items())  # order-strict
            assert dotenv_rust.dotenv_rust_disabled() is False, (
                "a burn-in/latch trip inside the differential marks a real divergence"
            )

        _differential()


# ── Workflow wiring pins ────────────────────────────────────────────────────


class TestWorkflowWiring:
    _WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

    def test_mutation_pair_wired(self) -> None:
        text = (self._WORKFLOWS / "mutation.yml").read_text(encoding="utf-8")
        pair = '"brutex/config/dotenv_rust.py:tests/unit/test_config_dotenv_rust.py:"'
        assert pair in text, (
            "dotenv_rust.py is not wired into mutation.yml's per-module mutmut loop"
        )

    def test_parity_lane_carries_the_dotenv_flag_on_leg(self) -> None:
        text = (self._WORKFLOWS / "core-rust-parity.yml").read_text(encoding="utf-8")
        assert "BRUTEX_CORE_RUST_DOTENV" in text, "core-rust-parity.yml lost the dotenv flag-ON leg"
        assert "tests/unit/test_config_dotenv_rust.py" in text, (
            "core-rust-parity.yml no longer runs the dotenv facade suite"
        )
