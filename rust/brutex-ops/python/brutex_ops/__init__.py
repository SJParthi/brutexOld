"""brutex_ops — BruteX ops/automation Rust kernels (PyO3 bridge).

The UNIFIED ops crate — three lanes, one wheel (the
``integration/brutex-ops-unified`` vehicle): mixed Python + Rust layout (the
``brutex_feed`` shape). The compiled cdylib ships as the private submodule
``brutex_ops._brutex_ops``; this ``__init__.py`` re-exports its symbols so
each consumer facade writes ``import brutex_ops; brutex_ops.<kernel>(...)``.

Exposed surface (primitives only — ints, bools, floats, strings, bytes,
``None``), one block per lane:

* backup lane (``DEC-BACKUP-RUST-OPS-001``; facade
  ``brutex/backup/backup_rust.py``): ``evaluate_deadman_decision`` — the
  dead-man verdict state machine over Python-precomputed ``total_seconds()``
  deltas; ``render_backup_plist`` / ``render_deadman_plist`` /
  ``render_drill_plist`` / ``render_cron_lines`` — byte-exact
  ``schedule_render.py`` twins; ``parse_backup_summary_snapshot_id`` /
  ``parse_snapshots_latest_decision`` / ``parse_ls_file_paths`` /
  ``parse_ls_snapshot_time_decision`` / ``build_backup_argv`` — restic
  ``--json`` parse + argv twins (every abstain / raise-shaped outcome is an
  ORACLE code the facade maps back to the verbatim Python implementation).
* crossverify lane (``DEC-CROSSVERIFY-RUST-CORE-001``; facade
  ``brutex/crossverify/rust_seam.py``): ``crossverify_render_csv`` — the
  pinned CSV byte assembly over pre-validated, pre-formatted cell rows;
  ``crossverify_canonical_identity`` — the deny-by-default lake-coordinate →
  canonical-identity mapping; ``crossverify_s3_key`` — the ONE S3 key law.
* nightly lane (``DEC-NIGHTLY-RUST-CORE-001``; facade
  ``brutex/nightly/nightly_rust.py``): ``nightly_first_breaker_index`` /
  ``nightly_exit_code`` / ``nightly_audit_reconcile_detail`` — the chain
  break/exit-code + audit-reconcile laws; ``nightly_count_text`` /
  ``nightly_verdict`` / ``nightly_summary_lines`` — the verdict/Telegram
  summary builders; ``nightly_xml_escape`` / ``nightly_render_launchd_plist``
  / ``nightly_parse_at`` / ``nightly_pmset_wake_command`` /
  ``nightly_seconds_since_fire`` — the schedule render + arithmetic laws.
* ``version`` / ``SOURCE_DIGEST`` / ``__source_digest__`` — build stamps;
  the digest is the crate-source BLAKE3 baked by ``build.rs``
  (DEC-RUST-SOURCE-DIGEST-TWIN-001, MIGRATION.md #1258) covering ALL three
  lanes' sources under the ONE scheme.

Wheel-source freshness handshake (the brutex_feed pattern)
----------------------------------------------------------
Import-time guard: the compiled ``.so`` is asserted to expose EVERY symbol
this shim re-exports (``_EXPECTED_COMPILED_SYMBOLS``) BEFORE the re-export
runs, so a stale installed wheel that predates a source-tree symbol raises a
clear, actionable ``ImportError`` naming the missing symbol(s). The CONSUMER
side is soft: all three default-OFF facades catch this ImportError and
degrade to their byte-identical Python oracles (``crate_unavailable``
warn-once), so a stale wheel can never fail a backup, a cross-verify run or
a nightly. (UNIFICATION RULING: the crossverify lane copy shipped without
the import-time handshake — the facade-side ``_CRATE_API`` guard posture;
the unified crate keeps the STRICTER #1669 handshake over the full 27-symbol
union, which subsumes that posture — the facade guards still run.)

HONEST residual gap (Golden Rule #10, the brutex_feed envelope verbatim):
the version + symbol-set handshake catches a stale wheel that dropped/added
a symbol or changed crate version; same-version symbol-compatible BEHAVIORAL
drift is caught only by each facade's per-law burn-in bit-compare — after
burn-in a drifted wheel would serve silently until the next process.
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
    # ── backup lane ──
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
    # ── crossverify lane ──
    "crossverify_canonical_identity",
    "crossverify_render_csv",
    "crossverify_s3_key",
    # ── nightly lane ──
    "nightly_audit_reconcile_detail",
    "nightly_count_text",
    "nightly_exit_code",
    "nightly_first_breaker_index",
    "nightly_parse_at",
    "nightly_pmset_wake_command",
    "nightly_render_launchd_plist",
    "nightly_seconds_since_fire",
    "nightly_summary_lines",
    "nightly_verdict",
    "nightly_xml_escape",
    # ── shared plumbing ──
    "version",
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
    crossverify_canonical_identity,
    crossverify_render_csv,
    crossverify_s3_key,
    evaluate_deadman_decision,
    nightly_audit_reconcile_detail,
    nightly_count_text,
    nightly_exit_code,
    nightly_first_breaker_index,
    nightly_parse_at,
    nightly_pmset_wake_command,
    nightly_render_launchd_plist,
    nightly_seconds_since_fire,
    nightly_summary_lines,
    nightly_verdict,
    nightly_xml_escape,
    parse_backup_summary_snapshot_id,
    parse_ls_file_paths,
    parse_ls_snapshot_time_decision,
    parse_snapshots_latest_decision,
    render_backup_plist,
    render_cron_lines,
    render_deadman_plist,
    render_drill_plist,
    version,
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
    "crossverify_canonical_identity",
    "crossverify_render_csv",
    "crossverify_s3_key",
    "evaluate_deadman_decision",
    "nightly_audit_reconcile_detail",
    "nightly_count_text",
    "nightly_exit_code",
    "nightly_first_breaker_index",
    "nightly_parse_at",
    "nightly_pmset_wake_command",
    "nightly_render_launchd_plist",
    "nightly_seconds_since_fire",
    "nightly_summary_lines",
    "nightly_verdict",
    "nightly_xml_escape",
    "parse_backup_summary_snapshot_id",
    "parse_ls_file_paths",
    "parse_ls_snapshot_time_decision",
    "parse_snapshots_latest_decision",
    "render_backup_plist",
    "render_cron_lines",
    "render_deadman_plist",
    "render_drill_plist",
    "version",
]
