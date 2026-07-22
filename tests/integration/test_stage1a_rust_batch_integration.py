"""Integration nets for the batch Rust Stage-1A arm (DEC-STAGE1A-RUST-BATCH-001 §6.9).

Two REAL end-to-end proofs on top of the unit/property/golden layers:

* **Fire-test flag-ON twin** — the B1 engineered script that fires all six
  resurrected bits (VWAP 52-53 + BOS/CHoCH 56-59) through the real ``push()``
  path fires them IDENTICALLY through the flag-ON batch arm.
* **The G-5 second half** — a REAL 2-worker SPAWN ``run_parallel_grid`` with
  ``BRUTEX_SWEEP_RUST_STAGE1A=1`` produces a byte-identical results tree to
  the flag-OFF SEQUENTIAL supervised run, and the workers' own
  ``rust_stage1a.engaged`` INFO on the captured child fds proves the env flag
  PROPAGATED into the spawn children (never a parent-only illusion).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.engine.rust_stage1a import (
    STAGE1A_RUST_ENV,
    reset_observability_memo_for_tests,
    reset_self_check_for_tests,
)
from brutex.engine.stage1a import build_bar_bits
from structlog.testing import capture_logs

# Cross-imports of frozen suites' helpers (the tests.unit precedent) —
# consumed read-only; no fixture is edited or regenerated.
from tests.integration.test_stage1a_vwap_bos_bits_fire import (
    _HLC_SCRIPT,  # pyright: ignore[reportPrivateUsage]
    _bar,  # pyright: ignore[reportPrivateUsage]
    _fired_bits,  # pyright: ignore[reportPrivateUsage]
    _run,  # pyright: ignore[reportPrivateUsage]
)


@pytest.fixture(autouse=True)
def _fresh_seam_state(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    reset_self_check_for_tests()
    reset_observability_memo_for_tests()
    monkeypatch.delenv(STAGE1A_RUST_ENV, raising=False)
    yield
    reset_self_check_for_tests()
    reset_observability_memo_for_tests()


def test_fire_script_six_bits_fire_identically_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bits 52-53 + 56-59 fire at the SAME bars under the batch arm."""
    from brutex.vocabulary import bits as B

    bars = [_bar(m, h, low, c) for m, (h, low, c) in enumerate(_HLC_SCRIPT)]
    push_masks = _run(bars)  # the real per-bar push() path
    monkeypatch.setenv(STAGE1A_RUST_ENV, "1")
    with capture_logs() as logs:
        batch_masks = build_bar_bits(bars)
    assert [e for e in logs if e.get("event") == "rust_stage1a.engaged"]
    assert batch_masks == push_masks
    for bit in (
        B.BIT_CLOSE_ABOVE_VWAP,
        B.BIT_CLOSE_BELOW_VWAP,
        B.BIT_BOS_BULLISH,
        B.BIT_BOS_BEARISH,
        B.BIT_CHOCH_BULLISH,
        B.BIT_CHOCH_BEARISH,
    ):
        fired = _fired_bits(batch_masks, bit)
        assert fired, f"bit {bit} never fired on the batch arm"
        assert fired == _fired_bits(push_masks, bit)


@pytest.mark.slow
def test_flag_on_2worker_spawn_equals_flag_off_sequential_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """G-5 second half: REAL spawn workers, env-propagated, byte-identical.

    ``capfd`` captures at the fd level, so the ``rust_stage1a.engaged`` INFO
    asserted below is the CHILD processes' actual output — the proof the env
    flag reached the spawn workers (whose interpreters start clean; only the
    inherited ``os.environ`` carries the flag) and their builds genuinely ran
    the Rust arm, never a parent-only vacuous equality.
    """
    from tests.unit.test_engine_sweep_parallel import (
        _grid,  # pyright: ignore[reportPrivateUsage]
        _run_parallel,  # pyright: ignore[reportPrivateUsage]
        _run_sequential,  # pyright: ignore[reportPrivateUsage]
        _tree,  # pyright: ignore[reportPrivateUsage]
    )

    # The flag-OFF reference arm is the explicit "0" kill switch — since
    # DEC-STAGE1A-RUST-DEFAULT-ON-001 an unset env falls to the flipped
    # default-ON constant (the prior delenv arm would compare Rust to Rust).
    monkeypatch.setenv(STAGE1A_RUST_ENV, "0")
    seq_dir = tmp_path / "seq"
    seq_dir.mkdir()
    seq_out, _, _ = _run_sequential(seq_dir, _grid())

    monkeypatch.setenv(STAGE1A_RUST_ENV, "1")
    par_dir = tmp_path / "par"
    par_dir.mkdir()
    par_out, _, _ = _run_parallel(par_dir, _grid(), workers=2)
    captured = capfd.readouterr()
    assert "rust_stage1a.engaged" in captured.err + captured.out, (
        "no spawn worker logged rust_stage1a.engaged — the env flag did not "
        "propagate (or the batch arm silently fell back): a vacuous equality"
    )
    assert "rust_stage1a.kernel_autoreverted" not in captured.err + captured.out

    # The results trees are byte-identical: flag-ON parallel == flag-OFF
    # sequential (masks -> Stage-1B -> trades -> persisted bytes).
    assert _tree(seq_out) == _tree(par_out)
