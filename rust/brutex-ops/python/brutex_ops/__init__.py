"""brutex_ops — ops (backup) decision kernel (PyO3 bridge).

``DEC-BACKUP-RUST-OPS-001``: mixed Python + Rust layout (the ``brutex_feed``
shape). The compiled cdylib ships as the private submodule
``brutex_ops._brutex_ops``; this ``__init__.py`` re-exports its symbols so
the consumer (``brutex/backup/backup_rust.py``) writes ``import brutex_ops;
brutex_ops.evaluate_deadman_decision(...)``.

Exposed surface (primitives only — ints, bools, floats, strings, ``None``):

* ``evaluate_deadman_decision`` — the dead-man verdict state machine over
  Python-precomputed ``total_seconds()`` deltas; returns
  ``(arm, verdict, should_alert, hours_since_success, elapsed)``.
* ``render_backup_plist`` / ``render_deadman_plist`` / ``render_drill_plist``
  / ``render_cron_lines`` — byte-exact ``schedule_render.py`` twins.
* ``parse_backup_summary_snapshot_id`` / ``parse_snapshots_latest_decision``
  / ``parse_ls_file_paths`` / ``parse_ls_snapshot_time_decision`` /
  ``build_backup_argv`` — restic ``--json`` parse + argv twins; every
  abstain / raise-shaped outcome is an ORACLE code the facade maps back to
  the verbatim Python implementation.
* ``SOURCE_DIGEST`` / ``__source_digest__`` — the crate-source BLAKE3 digest
  baked by ``build.rs`` (DEC-RUST-SOURCE-DIGEST-TWIN-001, MIGRATION.md #1258).

Wheel-source freshness handshake (the brutex_feed pattern)
----------------------------------------------------------
Import-time guard: the compiled ``.so`` is asserted to expose EVERY symbol
this shim re-exports (``_EXPECTED_COMPILED_SYMBOLS``) BEFORE the re-export
runs, so a stale installed wheel that predates a source-tree symbol raises a
clear, actionable ``ImportError`` naming the missing symbol(s). The CONSUMER
side is soft: the default-OFF ``brutex/backup/backup_rust.py`` facade
catches this ImportError and degrades to the byte-identical Python oracle
(``crate_unavailable`` warn-once), so a stale wheel can never fail a backup.

HONEST residual gap (Golden Rule #10, the brutex_feed envelope verbatim):
the version + symbol-set handshake catches a stale wheel that dropped/added
a symbol or changed crate version; same-version symbol-compatible BEHAVIORAL
drift is caught only by the facade's per-law 16-call burn-in bit-compare —
after burn-in a drifted wheel would serve silently until the next process.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence

#: Every symbol this shim re-exports from the compiled PyO3 module — the ONE
#: in-code source of truth for the import-time freshness handshake. The
#: in-repo ``EXPECTED_BUILD.json`` mirrors the non-dunder names; a test pins
#: the two equal so they cannot drift.
_EXPECTED_COMPILED_SYMBOLS: tuple[str, ...] = (
    "SOURCE_DIGEST",
    "__version__",
    "build_backup_argv",
    "evaluate_deadman_decision",
    "parse_backup_summary_snapshot_id",
    "parse_ls_file_paths",
    "parse_ls_snapshot_time_decision",
    "parse_snapshots_latest_decision",
    "render_backup_plist",
    "render_cron_lines",
    "render_deadman_plist",
    "render_drill_plist",
)


def _assert_compiled_surface(
    compiled: object,
    expected: Sequence[str] = _EXPECTED_COMPILED_SYMBOLS,
) -> None:
    """Raise an actionable ``ImportError`` if ``compiled`` misses any expected symbol.

    The teeth of the wheel-source freshness handshake: a symbol-incomplete
    (stale) compiled module fails HERE, at import, naming exactly what is
    missing and how to fix it — never a confusing ``ImportError: cannot
    import name ...`` deep inside a consumer.
    """
    missing = sorted(name for name in expected if not hasattr(compiled, name))
    if missing:
        location = getattr(compiled, "__file__", "<unknown location>")
        raise ImportError(
            "brutex_ops: the installed compiled wheel "
            f"(brutex_ops._brutex_ops at {location}) is STALE — it is missing "
            f"symbol(s) {', '.join(missing)} that this source tree's shim "
            "re-exports. Rebuild + reinstall the wheel from the current source: "
            "run `maturin develop --release` inside rust/brutex-ops "
            "(with the project venv active)."
        )


_compiled = importlib.import_module("._brutex_ops", __name__)
_assert_compiled_surface(_compiled)

# The freshness handshake above MUST run before this re-export so a stale
# wheel raises the actionable message, not a bare from-import failure.
from ._brutex_ops import (  # noqa: E402
    SOURCE_DIGEST,
    __version__,
    build_backup_argv,
    evaluate_deadman_decision,
    parse_backup_summary_snapshot_id,
    parse_ls_file_paths,
    parse_ls_snapshot_time_decision,
    parse_snapshots_latest_decision,
    render_backup_plist,
    render_cron_lines,
    render_deadman_plist,
    render_drill_plist,
)

#: Crate version of the INSTALLED compiled kernel — derived at import from the
#: compiled module's ``__version__`` (``env!("CARGO_PKG_VERSION")`` baked into
#: the ``.so`` at build time), NEVER hardcoded here. Deliberately NOT in
#: ``__all__`` (mirrors the siblings): a build stamp, not kernel API.
__build_crate_version__: str = __version__

#: Crate-source BLAKE3 digest of the INSTALLED compiled kernel (FRAMING v1;
#: DEC-RUST-SOURCE-DIGEST-TWIN-001). Same value as ``SOURCE_DIGEST`` — the
#: dunder spelling mirrors the sibling wheels' doctor-facing surface.
__source_digest__: str = SOURCE_DIGEST

__all__ = [
    "SOURCE_DIGEST",
    "__version__",
    "build_backup_argv",
    "evaluate_deadman_decision",
    "parse_backup_summary_snapshot_id",
    "parse_ls_file_paths",
    "parse_ls_snapshot_time_decision",
    "parse_snapshots_latest_decision",
    "render_backup_plist",
    "render_cron_lines",
    "render_deadman_plist",
    "render_drill_plist",
]
