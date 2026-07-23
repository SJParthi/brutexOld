"""Default-ON Rust seam (DEC-RUST-DEFAULT-ON-WAVE-001) for the :mod:`brutex.config.backup_env` shape law.

DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943), milestone 3. ONE routed
surface — when ``BRUTEX_CORE_RUST_BACKUPENV`` is ON, the restic
``--read-data-subset`` shape validation (``_valid_verify_subset`` —
DEC-BACKUP-OFFBOX-001 round-7/round-9) delegates to the ``brutex_core``
crate (``rust/brutex-core/src/backupenv.rs``) through this facade; the
flag-OFF default is the verbatim CPython oracle, behavior-identical AND
crate-untouched (bomb-crate spy-pinned).

Honesty banner (ADR-0020, mandatory verbatim): expected payoff ≈ 0 —
conversion is for uniformity, not speed. The law runs once per backup
config resolve; flag-ON ADDS an env read, an eligibility gate and an FFI
hop — a small slowdown, shipped INERT (a default flip needs its own DEC).
Flag-OFF cost disclosure (the money_rust/hardware_envelope_rust mirror):
the DEFAULT (flag-OFF) path pays a per-call env probe —
``os.environ.get`` on a miss is ~700 ns/call on the build box — which is
DELIBERATE (spawn workers must read their own inherited environment;
caching the resolve would break that contract) and immaterial at
config-resolution call frequency.

## THE SEAM SPLIT (the honest convertible core — recorded here)

CONVERTED: the ONE pure shape predicate — the module's only genuine
side-effect-free LAW (three restic forms + probed bounds incl. the Go
``int64`` overflow and the CPython ``re`` ``$``-before-one-trailing-
newline quirk, both mirrored + cargo-pinned crate-side).

DELIBERATELY STAYS PYTHON (each with its reason): the WHOLE
``resolve_backup_env`` resolver and its numeric/keep/weekday/path
helpers — their fail-safe malformed-value WARN emissions are
observability side effects woven INTO the law (routing them would either
drop or double the WARNs); ``_optional``/``_split_excludes`` (trivial —
the smaller honest seam); ``_expand_user_path`` (the OS user database).

## The ELIGIBILITY GATE LAW (layer 2)

Only exact-type :class:`str` values, ASCII-only and ``<=`` 4096 bytes
may cross (the Python regexes are ASCII-strict so a non-ASCII value is
False on both sides, but the gate keeps the crate's proven domain
closed); anything else → oracle, crate untouched (spy-pinned). The crate
returns exactly a :class:`bool` (refuse-poison shape law,
exact-type-checked).

Fail-closed layers 3-6 live in the shared
:class:`brutex.config.seam_core.ConfigRustSeam` (one instance per seam —
the milestone-3 §11 COMMON hoist recorded there).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from brutex.config.seam_core import BURN_IN_SAMPLES, ConfigRustSeam

if TYPE_CHECKING:
    from collections.abc import Callable

#: The env flag (default ON per DEC-RUST-DEFAULT-ON-WAVE-001; explicit
#: falsy is the kill switch). Canonical ``BRUTEX_<CRATE>_RUST_<SEAM>`` form.
BACKUPENV_RUST_ENV: Final[str] = "BRUTEX_CORE_RUST_BACKUPENV"

#: The one law token (burn-in budget + reason prefix).
_LAW_VERIFY_SUBSET: Final[str] = "verify_subset"

#: The crate callable this seam requires (the API-surface handshake).
_CRATE_API: Final[tuple[str, ...]] = ("backupenv_verify_subset_valid",)

#: Eligibility bound — mirrors the crate's ``MAX_VALUE_LEN``.
_MAX_VALUE_LEN: Final[int] = 4096

_SEAM: Final[ConfigRustSeam] = ConfigRustSeam(
    seam="backup_env",
    env_var=BACKUPENV_RUST_ENV,
    crate_api=_CRATE_API,
    laws=(_LAW_VERIFY_SUBSET,),
    # DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive 2026-07-23): the
    # seam default is ON; explicit falsy stays the kill switch.
    default_on=True,
)


def backupenv_rust_enabled() -> bool:
    """Resolve ``BRUTEX_CORE_RUST_BACKUPENV`` (default ON per DEC-RUST-DEFAULT-ON-WAVE-001, junk loud-OFF)."""
    return _SEAM.enabled()


def backupenv_rust_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _SEAM.disabled()


def reset_backupenv_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset latch + burn-in budget + warn memo (test isolation ONLY)."""
    _SEAM.reset_for_tests(remaining=remaining)


def _eligible_value(value: object) -> bool:
    """Exact-type ASCII str within the crate's length bound (gate law)."""
    return type(value) is str and len(value) <= _MAX_VALUE_LEN and value.isascii()


def _valid_bool(value: object) -> bool:
    """Shape law for the crate return: exact-type :class:`bool` only."""
    return type(value) is bool


def route_verify_subset(
    value: str,
    python_law: Callable[[str], bool],
) -> bool:
    """The routed subset-shape law — every path returns the verdict."""
    if _SEAM.disabled() or not _eligible_value(value):
        return python_law(value)
    adopted = _SEAM.adopt(
        _LAW_VERIFY_SUBSET,
        "backupenv_verify_subset_valid",
        (value,),
        _valid_bool,
        lambda: python_law(value),
    )
    if adopted is None:
        return python_law(value)
    return adopted


__all__ = [
    "BACKUPENV_RUST_ENV",
    "backupenv_rust_disabled",
    "backupenv_rust_enabled",
    "reset_backupenv_rust_for_tests",
    "route_verify_subset",
]
