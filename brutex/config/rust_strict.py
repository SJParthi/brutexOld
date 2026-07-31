"""The ONE strict-Rust gate: turn every silent Python fall-back into a refusal.

DEC-RUST-STRICT-NO-FALLBACK-001 (operator directive: *"without any fucking
fallback python — only RUST O(1)"*).

Every ENGAGED Rust facade in this repo shares one shape: the Rust arm is tried,
and when it cannot serve, the facade calls its own ``warn_fallback_once`` /
``disable_kernel`` and returns a sentinel so the caller runs the retained
Python oracle. That funnel is not an accident — it is the property the degrade
sentinel (:mod:`brutex.observability.rust_census`) relies on: **a fall-back
must announce itself exactly once**, so "zero announcements" proves "nothing
fell back".

This module inverts that funnel. Because every fall-back is already forced to
announce itself through one function per facade, converting *announce* into
*raise* at that same point is provably complete — there is no unannounced path
left for Python to serve through. One edit per facade, at the choke point the
facade already has.

Polarity note (this deliberately inverts the house junk-env law)
---------------------------------------------------------------
Elsewhere in BruteX a junk env value resolves **fail-safe OFF** — a typo must
never silently arm an experimental kernel. Here "safe" points the other way:
under a no-fallback mandate the dangerous outcome is silently *re-enabling*
Python, so a junk value keeps strict mode **ON** and the resulting refusal
names the junk. The kill switch is therefore explicit-falsy only.

Scope note (honest)
-------------------
Strict mode refuses every fall-back reason a facade reports, but the reasons
are not one kind of thing:

* **Rust is absent or broke** (``wheel_missing``, ``wheel_stale``,
  ``kernel_error``, ``*_error``) — refusing is exactly the intent.
* **Rust answered wrongly** (``burn_in_mismatch``, ``kernel_disabled``,
  ``*_result_refused``) — refusing is correct; a wrong Rust answer must not be
  papered over by a quiet Python re-run.
* **The input is outside the crate's envelope** (``*_input_refused``,
  ``nonexact_key_refused``, ``*_beyond_crate``) — Python was always the
  intended handler for these. Refusing them is a real behaviour change, and
  the honest fix is to widen the crate until the refusal is unreachable, not
  to crash on a legitimate input. Facades that can emit this class are wired
  to strict mode only after their envelope is widened or the class is proven
  unreachable on the production path.

Everything here is O(1) per call: one ``os.environ`` probe, no memo, no global
state (a cached verdict would leak across tests that set the env per case).
"""

from __future__ import annotations

from typing import Final, NoReturn

from brutex.config.env_flags import env_flag_tristate
from brutex.models import BruteXError

#: The one kill switch. Default ON — see :data:`RUST_STRICT_DEFAULT`.
RUST_STRICT_ENV: Final = "BRUTEX_RUST_STRICT"

#: Strict mode ships **ON** (DEC-RUST-STRICT-NO-FALLBACK-001). The one-constant
#: revert lever: flipping this to ``False`` restores the warn-and-degrade
#: behaviour repo-wide without touching a single facade.
RUST_STRICT_DEFAULT: Final = True


class RustFallbackRefused(BruteXError):
    """A Rust facade could not serve and strict mode forbids the Python arm.

    Carries the facade module, the facade's own fall-back *reason* token, and
    whatever bounded context the facade already logged — so the refusal reads
    exactly like the WARNING it replaces, only fatal.
    """


def strict_mode() -> bool:
    """Whether Python fall-backs are forbidden this process.

    * explicit falsy (``0/false/no/off``) → ``False`` — the operator kill
      switch, the ONLY way to re-enable Python fall-backs;
    * explicit truthy (``1/true/yes/on``) → ``True``;
    * unset / whitespace-empty → :data:`RUST_STRICT_DEFAULT` (**True**);
    * any other value → ``True`` (the inverted junk polarity documented in the
      module docstring — a typo'd kill switch must not silently re-enable
      Python).

    Read per call (one ``os.environ`` probe); deliberately not memoized.
    """
    import os

    raw = os.environ.get(RUST_STRICT_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        # Junk: keep strict. The refusal message names the junk so the operator
        # sees the typo rather than silently getting Python back.
        return True
    return RUST_STRICT_DEFAULT


def refuse(facade: str, reason: str, **context: object) -> NoReturn:
    """Raise the strict-mode refusal for *facade* / *reason*.

    Called from a facade's fall-back choke point when :func:`strict_mode` is
    on. The message names the facade, the reason, the bounded context, and the
    one kill switch — never a silent degrade.

    Raises
    ------
    RustFallbackRefused
        Always.
    """
    detail = ", ".join(f"{k}={v!r}" for k, v in sorted(context.items()))
    suffix = f" ({detail})" if detail else ""
    raise RustFallbackRefused(
        f"{facade} cannot serve this call in Rust (reason: {reason}){suffix}. "
        f"{RUST_STRICT_ENV} forbids the Python fall-back "
        "(DEC-RUST-STRICT-NO-FALLBACK-001). Fix the Rust arm — or set "
        f"{RUST_STRICT_ENV}=0 to re-enable the Python oracle."
    )


def refuse_if_strict(facade: str, reason: str, **context: object) -> None:
    """Refuse when strict mode is on; otherwise return so the caller degrades.

    The one-line insert every wired facade puts at the top of its existing
    ``warn_fallback_once`` / degrade choke point.
    """
    if strict_mode():
        refuse(facade, reason, **context)


__all__ = [
    "RUST_STRICT_DEFAULT",
    "RUST_STRICT_ENV",
    "RustFallbackRefused",
    "refuse",
    "refuse_if_strict",
    "strict_mode",
]
