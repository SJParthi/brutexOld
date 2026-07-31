"""Default-ON Rust seam (DEC-RUST-DEFAULT-ON-WAVE-001) for the :mod:`brutex.config.dotenv` LINE-PARSE law.

DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943), milestone 3. ONE routed
surface — when ``BRUTEX_CORE_RUST_DOTENV`` is ON, ``parse_dotenv``
delegates to the ``brutex_core`` crate
(``rust/brutex-core/src/dotenv.rs``) through this facade; the flag-OFF
default is the verbatim CPython oracle, behavior-identical AND
crate-untouched (bomb-crate spy-pinned).

Honesty banner (ADR-0020, mandatory verbatim): expected payoff ≈ 0 —
conversion is for uniformity, not speed. The parse runs once per process
bootstrap over a tiny file; flag-ON ADDS an env read, an eligibility
gate and an FFI hop — a small slowdown, shipped INERT (a default flip
needs its own DEC). Flag-OFF cost disclosure (the
money_rust/hardware_envelope_rust mirror): the DEFAULT (flag-OFF) path
pays a per-call env probe — ``os.environ.get`` on a miss is ~700 ns/call
on the build box — which is DELIBERATE (spawn workers must read their
own inherited environment; caching the resolve would break that
contract) and immaterial at config-resolution call frequency.

## THE SEAM SPLIT (the honest convertible core — recorded here)

CONVERTED: the pure LINE-PARSE law only (``.env`` text → ordered
key/value decisions; the crate returns the deduped pairs — exactly
``list(parse_dotenv(text).items())`` — and the facade rebuilds the dict).

DELIBERATELY STAYS PYTHON: ``load_dotenv_file`` (file I/O),
``apply_dotenv`` (env MUTATION — the never-override law), the
candidate-path search and ``bootstrap_env``'s masked-key-names logging
(observability side effects).

## SECRET SAFETY (this seam's own law)

``.env`` VALUES can be operator secrets (Telegram tokens, S3 creds — the
module's own "never log values" law), so this seam is constructed with
``redact_values=True``: a burn-in mismatch or refuse-poison event logs
types/counts ONLY — never a parsed value. The crate itself never logs,
never writes, never keeps state (its module docs carry the same pledge).

## The ELIGIBILITY GATE LAW (layer 2)

Only exact-type :class:`str` text, ASCII-only and ``<=`` 1 MiB may cross
(the crate's ASCII strip/splitlines twins are proven on exactly that
domain — CPython's Unicode whitespace/linebreak sets diverge from Rust's
outside it; a real ``.env`` is ASCII and a few hundred bytes). Anything
else → oracle, crate untouched (spy-pinned).

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
DOTENV_RUST_ENV: Final[str] = "BRUTEX_CORE_RUST_DOTENV"

#: The one law token (burn-in budget + reason prefix).
_LAW_PARSE: Final[str] = "parse"

#: The crate callable this seam requires (the API-surface handshake).
_CRATE_API: Final[tuple[str, ...]] = ("dotenv_parse_pairs",)

#: Eligibility bound — mirrors the crate's ``MAX_TEXT_LEN`` (1 MiB).
_MAX_TEXT_LEN: Final[int] = 1 << 20

#: The pair-shape law's tuple arity.
_PAIR_ARITY: Final[int] = 2

_SEAM: Final[ConfigRustSeam] = ConfigRustSeam(
    seam="dotenv",
    env_var=DOTENV_RUST_ENV,
    crate_api=_CRATE_API,
    laws=(_LAW_PARSE,),
    redact_values=True,
    # DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive 2026-07-23): the
    # seam default is ON; explicit falsy stays the kill switch.
    default_on=True,
    # DEC-RUST-STRICT-NO-FALLBACK-001: opt this facade into strict mode. Every
    # ANNOUNCED fall-back (class A: crate absent/stale/panicked/raised + the
    # junk-flag arm; class B: refuse-poison + burn-in mismatch) raises under
    # BRUTEX_RUST_STRICT. `_eligible_text` (exact-type ASCII str within the
    # size cap) is CLASS C — the crate never claimed a non-ASCII/oversized
    # payload — and returns the Python law silently, so it is unaffected.
    strict_facade="brutex.config.dotenv_rust",
)


def dotenv_rust_enabled() -> bool:
    """Resolve ``BRUTEX_CORE_RUST_DOTENV`` (default ON per DEC-RUST-DEFAULT-ON-WAVE-001, junk loud-OFF)."""
    return _SEAM.enabled()


def dotenv_rust_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _SEAM.disabled()


def reset_dotenv_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset latch + burn-in budget + warn memo (test isolation ONLY)."""
    _SEAM.reset_for_tests(remaining=remaining)


def _eligible_text(text: object) -> bool:
    """Exact-type ASCII str within the crate's 1 MiB bound (gate law)."""
    return type(text) is str and len(text) <= _MAX_TEXT_LEN and text.isascii()


def _valid_pairs(value: object) -> bool:
    """Shape law for a ``dotenv_parse_pairs`` crate return.

    A list of exact 2-tuples of exact-type ASCII strs with non-empty
    keys — the crate's declared render range; anything else is refused
    (types/counts logged only — the redact law).
    """
    if type(value) is not list:
        return False
    for item in value:
        if type(item) is not tuple or len(item) != _PAIR_ARITY:
            return False
        key, val = item
        if type(key) is not str or type(val) is not str:
            return False
        if not key or not key.isascii() or not val.isascii():
            return False
    return True


def route_parse_dotenv(
    text: str,
    python_law: Callable[[str], dict[str, str]],
) -> dict[str, str]:
    """The routed ``parse_dotenv`` — every path returns the parsed dict.

    The burn-in compares the crate's ordered pairs against
    ``list(oracle.items())`` (order-STRICT — a dict ``==`` would ignore
    insertion order, hiding a first-insertion-position divergence).
    """
    if _SEAM.disabled() or not _eligible_text(text):
        return python_law(text)
    adopted = _SEAM.adopt(
        _LAW_PARSE,
        "dotenv_parse_pairs",
        (text,),
        _valid_pairs,
        lambda: list(python_law(text).items()),
    )
    if adopted is None:
        return python_law(text)
    return dict(adopted)


__all__ = [
    "DOTENV_RUST_ENV",
    "dotenv_rust_disabled",
    "dotenv_rust_enabled",
    "reset_dotenv_rust_for_tests",
    "route_parse_dotenv",
]
