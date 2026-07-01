# pyright: reportPrivateUsage=none
"""Unit test for the CLI boot-time auto-resume argv builder (DEC-AUTO-RESUME-ON-BOOT-001).

The dashboard boot wiring (:func:`brutex.cli._start_auto_resume_on_boot` /
:func:`brutex.cli._spawn_auto_resume_pull`) is live daemon-thread + subprocess +
server glue, excluded at the SAME live-side-effect boundary the supervisor's
daemon loop is (``# pragma: no cover``); the resume DECISION is 100%-tested in
:mod:`brutex.orchestrator.auto_resume`. The one PURE seam in the CLI — the argv
that launches the resuming ``brutex fetch --universe --until-clean`` child — is
un-pragma'd and pinned here (mirrors ``test_cli_supervisor`` for
``_supervisor_child_argv``).
"""

from __future__ import annotations

import sys
from pathlib import Path

from brutex import cli


def test_auto_resume_child_argv_is_universe_until_clean() -> None:
    """The resume child re-runs the full-universe pull until the ledger is clean."""
    argv = cli._auto_resume_child_argv(Path("/data/lake"))
    assert argv == [
        sys.executable,
        "-m",
        "brutex",
        "fetch",
        "--universe",
        "--until-clean",
        "--lake-root",
        "/data/lake",
    ]


def test_auto_resume_child_argv_carries_resolved_lake_root() -> None:
    """The child argv carries the SAME lake root the ledger was read from (MAJOR fix).

    Without the explicit ``--lake-root`` a dashboard started with
    ``--lake-root /data/lake`` (``$BRUTEX_LAKE_ROOT`` unset) would spawn a child
    that falls back to ``~/.brutex/lake`` — re-pulling the WRONG lake AND missing
    the lake-root-keyed single-flight lock (the double-pull this feature prevents).
    """
    argv = cli._auto_resume_child_argv(Path("/mnt/ssd/brutex-lake"))
    assert "--lake-root" in argv
    # The resolved root is the LAST token, directly after the ``--lake-root`` flag.
    assert argv[argv.index("--lake-root") + 1] == "/mnt/ssd/brutex-lake"
