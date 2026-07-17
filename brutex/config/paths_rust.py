"""Default-OFF Rust seam for the :mod:`brutex.config.paths` derivation laws.

DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943), milestone 3. The TWO
routed surfaces — when ``BRUTEX_CORE_RUST_PATHS`` is ON they delegate to
the ``brutex_core`` crate (``rust/brutex-core/src/paths.rs``) through
this facade; the flag-OFF default is the verbatim CPython oracle,
behavior-identical AND crate-untouched (bomb-crate spy-pinned):

* ``resolve_lake_root``'s DECISION law (a truthy override wins, else
  ``<home>/.brutex/lake`` — reduced to the probed ``PurePosixPath``
  normalization on every crossing input);
* ``lake_root_identity``'s volume-parent derivation (the G1
  ``/Volumes/<name>`` law; the crate's ``""`` return is the no-parent
  sentinel — an ``Option`` would collide with the adopt-core's ``None``
  = serve-the-oracle contract).

Honesty banner (ADR-0020, mandatory verbatim): expected payoff ≈ 0 —
conversion is for uniformity, not speed. Both laws are sub-microsecond
path ops; flag-ON ADDS an env read, an eligibility gate and an FFI hop
per call — a small slowdown, shipped INERT (a default flip needs its own
DEC). These laws run at config-resolution frequency (process start / a
handful per pull), never per bar.

## THE SEAM SPLIT (the honest convertible core — recorded here)

CONVERTED: exactly the two PURE string-derivation laws above.

DELIBERATELY STAYS PYTHON (each with its reason): the ``os.environ`` /
injected-mapping lookup (env access); ``Path.home()`` resolution (env +
OS); ``Path.expanduser()`` (the OS user database — ``~``-prefixed
overrides are eligibility-gated to the oracle wholesale, which also
preserves the exact ``RuntimeError`` a nonexistent ``~user`` raises
today); the ``ismount``/``stat`` probes and the `exists` check in
``lake_root_identity`` (filesystem I/O); the frozen ``LakeRootIdentity``
construction (only strings cross the FFI).

## The ELIGIBILITY GATE LAW (layer 2 — strings-only FFI)

Only values whose oracle outcome is provably reproduced by the crate's
ASCII string arithmetic may cross (anything else → oracle, crate
untouched, spy-pinned): overrides must be exact-type :class:`str`,
ASCII-only (a lone-surrogate ``os.environ`` value cannot even convert at
the PyO3 boundary), ``<=`` 4096 bytes and NOT ``~``-prefixed; crossing
``Path`` objects must be exact-type ``PosixPath`` (a subclass's
overridden ``__str__``/``__truediv__`` must reach the oracle) with an
ASCII, length-bounded ``str()`` render. The override arm never resolves
``home`` at all — mirroring the oracle's laziness (a broken ``$HOME``
must not raise on a path the oracle never touches).

Fail-closed layers 3-6 live in the shared
:class:`brutex.config.seam_core.ConfigRustSeam` (one instance per seam —
the milestone-3 §11 COMMON hoist recorded there).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from brutex.config.seam_core import BURN_IN_SAMPLES, ConfigRustSeam

if TYPE_CHECKING:
    from collections.abc import Callable

#: The opt-in env flag (default OFF; Pattern A — only explicit truthy
#: enables). Canonical ``BRUTEX_<CRATE>_RUST_<SEAM>`` form.
PATHS_RUST_ENV: Final[str] = "BRUTEX_CORE_RUST_PATHS"

#: The two law tokens (per-law burn-in budgets + reason prefixes).
_LAW_LAKE_ROOT: Final[str] = "lake_root"
_LAW_VOLUME_PARENT: Final[str] = "volume_parent"

#: The crate callables this seam requires (the API-surface handshake).
_CRATE_API: Final[tuple[str, ...]] = ("paths_lake_root_str", "paths_volume_parent_str")

#: Eligibility/shape bound — mirrors the crate's ``MAX_PATH_LEN``.
_MAX_PATH_LEN: Final[int] = 4096

#: Exact runtime Path type on this platform (``PosixPath``) — a subclass
#: routes to the oracle (the exact-type precedent).
_PATH_TYPE: Final[type[Path]] = type(Path())

#: The volume-parent render prefix (the crate's non-empty return shape).
_VOLUME_PREFIX: Final[str] = "/Volumes/"

_SEAM: Final[ConfigRustSeam] = ConfigRustSeam(
    seam="paths",
    env_var=PATHS_RUST_ENV,
    crate_api=_CRATE_API,
    laws=(_LAW_LAKE_ROOT, _LAW_VOLUME_PARENT),
)


def paths_rust_enabled() -> bool:
    """Resolve ``BRUTEX_CORE_RUST_PATHS`` (default OFF, junk loud-OFF)."""
    return _SEAM.enabled()


def paths_rust_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _SEAM.disabled()


def reset_paths_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset latch + burn-in budgets + warn memo (test isolation ONLY)."""
    _SEAM.reset_for_tests(remaining=remaining)


def _eligible_text(text: object) -> bool:
    """Exact-type ASCII str within the crate's length bound."""
    return type(text) is str and len(text) <= _MAX_PATH_LEN and text.isascii()


def _eligible_override(override: object) -> bool:
    """The override gate: ``None`` or an ASCII non-``~`` str (gate law)."""
    if override is None:
        return True
    if type(override) is not str:
        return False
    return _eligible_text(override) and not override.startswith("~")


def _eligible_path_obj(path: object) -> bool:
    """Exact-type ``PosixPath`` whose render passes the text gate."""
    return type(path) is _PATH_TYPE and _eligible_text(str(path))


def _valid_path_str(value: object) -> bool:
    """Shape law for a ``lake_root`` crate return: non-empty ASCII str."""
    return type(value) is str and 0 < len(value) <= _MAX_PATH_LEN and value.isascii()


def _valid_volume_parent_str(value: object) -> bool:
    """Shape law for a ``volume_parent`` crate return.

    Either the empty no-parent sentinel or ``/Volumes/<name>`` with a
    non-empty single-component name (the exact render range).
    """
    if type(value) is not str or len(value) > _MAX_PATH_LEN or not value.isascii():
        return False
    if value == "":
        return True
    name = value.removeprefix(_VOLUME_PREFIX)
    return name != value and bool(name) and "/" not in name


def route_lake_root(
    override: str | None,
    home_factory: Callable[[], Path],
    python_law: Callable[[], Path],
) -> Path:
    """The routed ``resolve_lake_root`` decision — every path returns the
    correct resolved ``Path``.

    *home_factory* resolves the home directory LAZILY — invoked only on
    the default arm, mirroring the oracle's laziness (module docstring);
    a raise from it is exactly the raise the oracle's own resolution
    would produce.
    """
    if _SEAM.disabled() or not _eligible_override(override):
        return python_law()
    if override:
        arguments: tuple[object, ...] = (override, "")
    else:
        home = home_factory()
        if not _eligible_path_obj(home):
            return python_law()
        arguments = (override, str(home))
    adopted = _SEAM.adopt(
        _LAW_LAKE_ROOT,
        "paths_lake_root_str",
        arguments,
        _valid_path_str,
        lambda: str(python_law()),
    )
    if adopted is None:
        return python_law()
    return Path(adopted)


def route_volume_parent(
    resolved: Path,
    python_law: Callable[[], Path | None],
) -> Path | None:
    """The routed volume-parent derivation — every path returns the G1
    ``/Volumes/<name>`` parent or ``None``.
    """
    if _SEAM.disabled() or not _eligible_path_obj(resolved):
        return python_law()

    def _oracle_canonical() -> str:
        oracle = python_law()
        return "" if oracle is None else str(oracle)

    adopted = _SEAM.adopt(
        _LAW_VOLUME_PARENT,
        "paths_volume_parent_str",
        (str(resolved),),
        _valid_volume_parent_str,
        _oracle_canonical,
    )
    if adopted is None:
        return python_law()
    return None if adopted == "" else Path(adopted)


__all__ = [
    "PATHS_RUST_ENV",
    "paths_rust_disabled",
    "paths_rust_enabled",
    "reset_paths_rust_for_tests",
    "route_lake_root",
    "route_volume_parent",
]
