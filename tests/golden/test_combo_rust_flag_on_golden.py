"""Flag-ON re-drive of the frozen run-identity goldens (DEC-STRATEGIES-RUST-COMBO-001 §4.1).

The frozen golden modules are NEVER edited — this wrapper imports them and
re-executes their byte pins with ``BRUTEX_STRATEGIES_RUST_COMBO=1`` in the
post-burn-in STEADY STATE (Rust bytes ship, no oracle cushion) under an
ENGAGEMENT SPY (the ``combo_rust.engaged`` INFO — anti-vacuous: a silently
OFF/latched run would pass the byte pins trivially). Every frozen literal —
``_GOLDEN_RUN_ID`` / ``_PRECISE_ERA_RUN_ID`` / ``_GOLDEN_CONTENT_HASH`` /
the 10-digest combo-hash corpus / the spec 57-byte + run-id pins / the
triple-route refinement / walk-forward digests — must reproduce
byte-for-byte with the Rust path genuinely serving. Requires the real
``brutex_strategies`` wheel (skipped honestly when absent; the CI golden
job installs it, so the wrapper EXECUTES there, never skip-only).
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import Any

import brutex.strategies.combo_rust as cr
import pytest
from structlog.testing import capture_logs

pytest.importorskip(
    "brutex_strategies",
    reason="brutex_strategies wheel not installed — the flag-ON golden wrapper needs the kernel",
)

import tests.golden.test_combo_hash_distinct_golden as combo_golden
import tests.golden.test_engine_run_identity_golden as identity_golden
import tests.golden.test_engine_sweep_run_golden as sweep_golden
import tests.golden.test_strategies_spec_golden as spec_golden


@pytest.fixture(autouse=True)
def _rust_on_steady(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(cr.STRATEGIES_RUST_COMBO_ENV, "1")
    cr.reset_combo_rust_for_tests(remaining=0)
    yield
    cr.reset_combo_rust_for_tests()


def _assert_engaged_and_live(logs: list[MutableMapping[str, Any]]) -> None:
    """The engagement spy: the Rust path served + never latched/degraded."""
    assert [e for e in logs if e["event"] == "combo_rust.engaged"], "Rust path never engaged"
    assert [e for e in logs if e["event"] == "combo_rust.fallback"] == []
    assert cr.rust_combo_disabled() is False


def test_golden_run_id_reproduces_flag_on() -> None:
    with capture_logs() as logs:
        sweep_golden.test_golden_run_id_is_byte_stable()
        sweep_golden.test_golden_run_id_never_aliases_the_retired_eras()
    _assert_engaged_and_live(logs)


def test_golden_content_hash_reproduces_flag_on(tmp_path: Path) -> None:
    with capture_logs() as logs:
        sweep_golden.test_golden_content_hash_literal(tmp_path)
    _assert_engaged_and_live(logs)


def test_combo_hash_distinct_corpus_reproduces_flag_on() -> None:
    with capture_logs() as logs:
        combo_golden.test_every_combo_matches_its_frozen_digest()
        combo_golden.test_all_digests_are_distinct()
        combo_golden.test_params_insertion_order_does_not_change_the_digest()
    _assert_engaged_and_live(logs)


def test_spec_goldens_reproduce_flag_on() -> None:
    with capture_logs() as logs:
        spec_golden.test_canonical_bytes_frozen_and_wheel_equals_native_literal()
        spec_golden.test_spec_run_id_frozen()
    _assert_engaged_and_live(logs)


def test_triple_route_identity_digests_reproduce_flag_on() -> None:
    # The engine mirrors route through the COMMON reject_unit_separator (the
    # scan seam) while their json stays engine-local — the frozen digests
    # must be byte-stable with the flag ON.
    with capture_logs() as logs:
        identity_golden.test_refinement_run_id_digests_frozen()
        identity_golden.test_walk_forward_run_hash_digests_frozen()
        identity_golden.test_walk_forward_schedule_hash_digests_frozen()
        identity_golden.test_all_frozen_digests_distinct()
    _assert_engaged_and_live(logs)
