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
Strict mode refuses almost every fall-back reason a facade reports, but the
reasons are not one kind of thing:

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
* **The class cannot be resolved from the crate source** — RESOLVED; the
  residual is closed. This bullet used to name six tokens whose crate arm
  could fault *or* refuse a wide input through the same ``Err``, so a
  facade could not tell class A/B from class C and kept warn-and-degrade
  rather than guess. All six are now split crate-side, so
  ``_STRICT_UNRESOLVED_REASONS`` is an EMPTY frozenset in all four facades
  that carry it (``rust_stage1a.py``, ``cvar_gate.py``, ``nightly_rust.py``,
  ``combo_rust.py``) and every reason token classifies.

  The split is one shared mechanism, not four ad-hoc ones. Each crate
  defines ``INPUT_REFUSED_MARKER = "input outside the crate envelope"`` and
  STAMPS it on every input-VALUE-envelope refusal it raises, leaving faults
  and marshalling bugs deliberately UNMARKED. The facade then classifies on
  evidence the crate itself supplied — ``_INPUT_REFUSED_MARKER in detail``
  is class C (degrade; Python was always the intended handler), anything
  else is class A/B (refuse). No facade parses natural language, and a
  crate that forgets to stamp fails CLOSED into a refusal rather than open
  into a silent Python re-run.

  The literal is part of the facade contract and is identical byte-for-byte
  in all four crates and all four facades:
  ``rust/trading-core/src/python.rs`` (DEC-RUST-STAGE1A-KERNEL-001),
  ``rust/fno-math/src/input_refused.rs`` (DEC-RANK-CVAR-TAIL-GATE-001),
  ``rust/brutex-ops/src/nightly.rs`` (DEC-NIGHTLY-RUST-CORE-001) and
  ``rust/brutex-strategies/src/python.rs``, whose lane did the split first
  and is the template the other three follow.

Honest limits — broad-except ABSORPTION (Golden Rule #10)
---------------------------------------------------------
The completeness argument above is a claim about the CHOKE POINT, not about
end-to-end delivery to the operator. :class:`RustFallbackRefused` derives from
``BruteXError`` → ``Exception``, so a refusal raised inside a region wrapped by
a broad ``except Exception`` is ABSORBED: the caller logs its own best-effort
WARNING and PROCEEDS. Strict mode still stopped the Python arm from serving —
the refusal is real and the fall-back did not happen — but the operator sees
that caller's degrade line, not a crash, and the enclosing operation reports
success.

Three sites were VERIFIED reachable + absorbing (2026-07-31, by forcing a real
crate fault so the facade hits its own choke point under
``BRUTEX_RUST_STRICT=1``). All three are now CLOSED: each grew an
``except RustFallbackRefused: raise`` escape placed BEFORE its broad guard,
mirroring the ``except LakeWriteHalt: raise`` precedent that already sat at
``lake_heal.py:1147``. ``LakeWriteHalt`` keeps its own escape and
``KeyboardInterrupt``/``SystemExit`` are untouched (``RustFallbackRefused`` is
an ``Exception``, disjoint from the ``BaseException``-only classes).

* ``brutex/data/lake_heal.py:1325`` (``_bump_stats``) — WAS logged as
  ``lake_heal.stats_write_failed`` with the heal-stats sidecar silently NOT
  written. Trade-off accepted: the sidecar is best-effort about its own I/O,
  not about the Rust arm being unable to serve at all.
* ``brutex/data/lake_heal.py:1158`` (``_rebuild_tf_months``, escaping ahead of
  ``_SWEEP_GUARD_ERRORS`` at :1160) — WAS logged as
  ``lake_heal.tf_rebuild_failed`` while the batch continued over TF months
  left quarantine-DELETED.
* ``brutex/data/spot_daily_resample.py:826`` (the per-target recall guard, NOT
  in lake_heal) — the sharpest edge of the class, and the reason this section
  exists: absorbed, the daily recall derived ZERO bars while
  ``lake_heal.sweep_complete`` still reported ``healed=N`` and STAMPED the
  swept-sentinel, so the next boot skipped the re-scan forever.

Sites VERIFIED **not** reachable (same method; the guarded callee reaches no
strict-wired facade, positive control passing): ``lake_heal.py:531``
(``_audit_append``), ``:2253`` (``open_lake_heal_audit``) and ``:2296``
(``open_lake_catalog``) — ``brutex/orchestrator/audit.py`` and
``brutex/data/catalog.py`` are stdlib/duckdb-only on those paths.

The ONE remaining absorbing site — deliberately left
--------------------------------------------------
``lake_heal.py`` ``StartupSweeper.run_sync`` (the "boot never crashes"
blanket) is now PROVEN reachable un-shadowed, two independent ways: the
sentinel ``atomic_write_simple`` in ``run_startup_sweep`` sits inside zero
``try`` blocks, and all three escapes above now re-raise from call sites that
have no enclosing guard. It is kept absorbing ON PURPOSE, and the reason is
the distinction this whole section is about: unlike the three sites above it
absorbs into an honest FAILURE, not a success claim — nothing is stamped, no
``sweep_complete`` is emitted, ``None`` is returned, and the refusal's full
message (facade, reason, kill switch) rides a structured
``lake_heal.sweep_failed`` WARNING. Flipping it would convert that into an
unhandled traceback in a daemon thread, losing the event name monitors key on
for zero correctness gain. A dedicated test pins the terminal so it can never
silently become a success. This is a judgement call, and it is the honest
partial: one of the four documented sites still absorbs, by design and with
teeth, rather than none.

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


#: Per-value ``repr`` cap for :func:`refuse` context. Matches the
#: ``repr(...)[:200]`` convention the facades use for their own log fields
#: (e.g. ``brutex/ranking/cvar_gate.py``), so a refusal message stays readable
#: whatever a caller hands over.
_CONTEXT_REPR_CAP: Final = 200


def refuse(facade: str, reason: str, **context: object) -> NoReturn:
    """Raise the strict-mode refusal for *facade* / *reason*.

    Called from a facade's fall-back choke point when :func:`strict_mode` is
    on. The message names the facade, the reason, the bounded context, and the
    one kill switch — never a silent degrade.

    ``context`` is caller-supplied and a facade may hand over a whole frame, a
    bar list or a kernel result, so every value is ``repr``-bounded to
    :data:`_CONTEXT_REPR_CAP` characters here — the same ``repr(...)[:200]``
    convention the facades already apply to their own log fields. Bounding at
    this one choke point covers every facade, so no call site can mint an
    unbounded exception message.

    Raises
    ------
    RustFallbackRefused
        Always.
    """
    detail = ", ".join(f"{k}={repr(v)[:_CONTEXT_REPR_CAP]}" for k, v in sorted(context.items()))
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
