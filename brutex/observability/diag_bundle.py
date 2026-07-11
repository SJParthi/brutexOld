"""One-file diagnostic bundle per run — zero manual log collection (R40, D-16).

Every run (success OR failure) auto-packages its forensic surface into ONE
zip at ``<results_root>/runs/<run_id>/diagnostic-bundle.zip``:

* EVERY file under ``runs/<run_id>/`` (manifest, exit-status,
  flight-recorder, verdicts, chain-map, ``*.stamp.json`` sidecars,
  discarded quarantine remnants) in ONE recursive pass — O(artifacts),
  HONESTLY O(n), never claimed O(1);
* optional *extra_files* copied under ``extra/`` (the run core passes the
  progress + checkpoint files, which live outside the run dir);
* a bounded tail of the structlog file sink (``log-tail.txt``, via the
  COMMON :func:`brutex.observability.log_tail.tail_lines` reader);
* ``env-fingerprint.json`` — hardware key, Python/platform versions, and
  every ``BRUTEX_*`` env var with SECRET REDACTION (D-16): any variable
  whose NAME contains ``TOKEN`` / ``SECRET`` / ``KEY`` (case-insensitive)
  has its VALUE replaced by ``[REDACTED]`` — a bundle is meant to be
  attached to reviews and chats, so a credential must be structurally
  unable to ride along;
* ``bundle-meta.json`` — run id, member count, and the honest list of
  members that could not be read (skipped, never silently absent).

Fail-soft law (I-3): the bundle is a terminal observability artifact — ANY
build failure logs ONE ``diag_bundle.build_failed`` WARN and returns ``None``;
a single unreadable member is skipped + counted, never fatal. The zip lands
via the COMMON atomic tmp→fsync→``os.replace`` so a kill mid-build never
leaves a torn bundle (failure matrix A15).
"""

from __future__ import annotations

import json
import os
import platform
import sys
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import structlog

from brutex.config.hardware_envelope import hardware_key
from brutex.data.atomic_io import atomic_write_simple
from brutex.engine.run_layout import run_dir
from brutex.observability.log_tail import clamp_max_lines, tail_lines

_LOG = structlog.get_logger(__name__)

__all__ = [
    "BUNDLE_FILENAME",
    "REDACTED_VALUE",
    "build_diagnostic_bundle",
    "bundle_path",
    "redact_env",
]

#: The bundle file name inside ``runs/<run_id>/`` (design §2).
BUNDLE_FILENAME: Final[str] = "diagnostic-bundle.zip"

#: The value substituted for a secret-named env var (D-16).
REDACTED_VALUE: Final[str] = "[REDACTED]"

#: Case-insensitive NAME substrings that mark an env var as secret (D-16).
_SENSITIVE_NAME_TOKENS: Final[tuple[str, ...]] = ("TOKEN", "SECRET", "KEY")

#: Only the project's own env surface is fingerprinted.
_ENV_PREFIX: Final[str] = "BRUTEX_"

#: Atomic-write temp prefix (see :func:`brutex.data.atomic_io.atomic_write_simple`)
#: — an in-flight temp must never be bundled.
_TMP_PREFIX: Final[str] = ".brutex-write-"


def bundle_path(results_root: Path, run_id: str) -> Path:
    """Return the bundle path — ``<results_root>/runs/<run_id>/diagnostic-bundle.zip``.

    Parameters
    ----------
    results_root
        The sweep results root.
    run_id
        The run identity (D-1 — the bundle is findable from the one id, R43).

    Returns
    -------
    Path
        The bundle location.
    """
    return run_dir(results_root, run_id) / BUNDLE_FILENAME


def redact_env(env: Mapping[str, str]) -> dict[str, str]:
    """Project *env* onto the fingerprint surface with secret redaction (D-16).

    Keeps only ``BRUTEX_*`` names (sorted, deterministic); a name containing
    ``TOKEN`` / ``SECRET`` / ``KEY`` (case-insensitive) keeps its KEY but has
    its VALUE replaced by :data:`REDACTED_VALUE` — the presence of the flag is
    forensic signal, the credential is not.

    Parameters
    ----------
    env
        The environment mapping (inject; production passes ``os.environ``).

    Returns
    -------
    dict[str, str]
        Sorted-by-name ``BRUTEX_*`` vars, secrets redacted.
    """
    out: dict[str, str] = {}
    for name in sorted(env):
        if not name.startswith(_ENV_PREFIX):
            continue
        upper = name.upper()
        if any(token in upper for token in _SENSITIVE_NAME_TOKENS):
            out[name] = REDACTED_VALUE
        else:
            out[name] = env[name]
    return out


def _env_fingerprint(env: Mapping[str, str]) -> dict[str, object]:
    """Build the ``env-fingerprint.json`` payload (hardware key best-effort)."""
    try:
        key = hardware_key()
    except Exception as exc:
        _LOG.warning("diag_bundle.hardware_key_failed", error=str(exc))
        key = "unknown"
    return {
        "hardware_key": key,
        "python": sys.version,
        "platform": platform.platform(),
        "brutex_env": redact_env(env),
    }


def _write_zip(dest: Path, members: Sequence[tuple[str, bytes]]) -> None:
    """Write *members* as a deflated zip at *dest* (the atomic-write callback)."""
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for arcname, data in members:
            archive.writestr(arcname, data)


def build_diagnostic_bundle(
    results_root: Path,
    *,
    run_id: str,
    log_file: Path | None = None,
    max_log_lines: int = 5000,
    extra_files: Sequence[Path] = (),
    env: Mapping[str, str] | None = None,
) -> Path | None:
    """Package the run's forensic surface into ONE zip — fail-soft (R40).

    ONE recursive pass over ``runs/<run_id>/`` (HONESTLY O(artifacts));
    unreadable members are skipped + listed in ``bundle-meta.json``. Runs on
    success AND failure paths; any whole-build failure returns ``None`` with
    ONE ``diag_bundle.build_failed`` WARN (I-3).

    Parameters
    ----------
    results_root
        The sweep results root.
    run_id
        The run identity.
    log_file
        The structlog file sink to tail into ``log-tail.txt`` (``None`` /
        missing → no tail member).
    max_log_lines
        Tail bound, clamped by the COMMON
        :func:`~brutex.observability.log_tail.clamp_max_lines` law.
    extra_files
        Files OUTSIDE the run dir to include under ``extra/`` (the run core
        passes the progress + checkpoint artifacts); missing ones are
        skipped + counted.
    env
        Environment mapping for the fingerprint (inject; default
        ``os.environ``).

    Returns
    -------
    Path | None
        The bundle path, or ``None`` on a WARNed failure.
    """
    try:
        rundir = run_dir(results_root, run_id)
        dest = bundle_path(results_root, run_id)
        members: list[tuple[str, bytes]] = []
        skipped: list[str] = []
        if rundir.is_dir():
            for path in sorted(rundir.rglob("*")):
                if not path.is_file():
                    continue
                if path.name == BUNDLE_FILENAME or path.name.startswith(_TMP_PREFIX):
                    continue
                arcname = path.relative_to(rundir).as_posix()
                try:
                    members.append((arcname, path.read_bytes()))
                except OSError as exc:
                    skipped.append(arcname)
                    _LOG.warning(
                        "diag_bundle.member_skipped", member=arcname, run_id=run_id, error=str(exc)
                    )
        for extra in extra_files:
            extra_path = Path(extra)
            arcname = f"extra/{extra_path.name}"
            try:
                members.append((arcname, extra_path.read_bytes()))
            except OSError as exc:
                skipped.append(arcname)
                _LOG.warning(
                    "diag_bundle.member_skipped", member=arcname, run_id=run_id, error=str(exc)
                )
        if log_file is not None:
            lines = tail_lines(Path(log_file), max_lines=clamp_max_lines(max_log_lines))
            if lines:
                members.append(("log-tail.txt", ("\n".join(lines) + "\n").encode("utf-8")))
        fingerprint = _env_fingerprint(os.environ if env is None else env)
        members.append(
            (
                "env-fingerprint.json",
                json.dumps(fingerprint, indent=2, sort_keys=True).encode("utf-8"),
            )
        )
        meta = {"run_id": run_id, "member_count": len(members) + 1, "skipped": skipped}
        members.append(
            ("bundle-meta.json", json.dumps(meta, indent=2, sort_keys=True).encode("utf-8"))
        )
        rundir.mkdir(parents=True, exist_ok=True)
        atomic_write_simple(dest, lambda tmp: _write_zip(tmp, members))
    except Exception as exc:
        _LOG.warning(
            "diag_bundle.build_failed",
            run_id=run_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None
    return dest
