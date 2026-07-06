"""CLI entrypoint polars thread-bound pins (DEC-THREAD-BUDGET-BOUNDS-001).

``brutex.cli.main`` must set ``POLARS_MAX_THREADS`` BEFORE the first ``import
polars`` in the process — polars reads the var exactly once, at its own first
import. That ordering can only be proven in a FRESH interpreter, so these pins
drive ``main(["status"])`` (a cheap polars-free command past the version fast path) in a subprocess and read back both the env var and
(in the strongest pin) polars' actual pool size.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import textwrap

import brutex.cli

_PROBE = textwrap.dedent(
    """
    import json, os, sys

    import brutex.cli as cli

    polars_loaded_before = "polars" in sys.modules
    rc = cli.main(["status"])
    print(
        json.dumps(
            {
                "rc": rc,
                "polars_loaded_before": polars_loaded_before,
                "env": os.environ.get("POLARS_MAX_THREADS"),
            }
        )
    )
    """
)

_POOL_PROBE = textwrap.dedent(
    """
    import json, os, sys

    import brutex.cli as cli

    cli.main(["status"])
    import polars as pl

    print(
        json.dumps(
            {"env": os.environ.get("POLARS_MAX_THREADS"), "pool": pl.thread_pool_size()}
        )
    )
    """
)


def _clean_env(**extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("POLARS_MAX_THREADS", "BRUTEX_POLARS_THREADS")
    }
    env.update(extra)
    return env


def _run(script: str, env: dict[str, str]) -> dict[str, object]:
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=120,
    )
    out: dict[str, object] = json.loads(proc.stdout.strip().splitlines()[-1])
    return out


class TestCliPolarsThreadBound:
    def test_main_wires_the_bound_before_dispatch(self) -> None:
        # Static wiring pin: main() must call apply_polars_thread_bound after
        # the polars-free `version` fast path (whose byte-exact stdout is
        # pinned elsewhere) but BEFORE any subcommand dispatch that can import
        # polars (the subprocess pins below prove the runtime ordering; this
        # catches a refactor that silently drops or reorders the call).
        source = inspect.getsource(brutex.cli._dispatch)
        assert "apply_polars_thread_bound()" in source
        assert source.index("cmd_version()") < source.index("apply_polars_thread_bound()")
        assert source.index("apply_polars_thread_bound()") < source.index('== "status"')

    def test_main_sets_polars_max_threads_before_polars_import(self) -> None:
        got = _run(_PROBE, _clean_env())
        assert got["rc"] == 0
        # The load-bearing precondition: importing brutex.cli must NOT import
        # polars (else the entrypoint bound would always be too late).
        assert got["polars_loaded_before"] is False
        assert got["env"] is not None
        assert int(str(got["env"])) >= 1

    def test_kill_switch_env_wins(self) -> None:
        got = _run(_PROBE, _clean_env(BRUTEX_POLARS_THREADS="3"))
        assert got["env"] == "3"

    def test_operator_preset_never_overwritten(self) -> None:
        got = _run(_PROBE, _clean_env(POLARS_MAX_THREADS="2"))
        assert got["env"] == "2"

    def test_polars_pool_actually_honours_the_bound(self) -> None:
        # The end-to-end teeth: polars imported AFTER main() sizes its global
        # pool to the bound (pin via the kill switch for a machine-independent
        # expected value).
        got = _run(_POOL_PROBE, _clean_env(BRUTEX_POLARS_THREADS="1"))
        assert got["env"] == "1"
        assert got["pool"] == 1
