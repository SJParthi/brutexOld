"""Run-level sentinel: make a silent fall-back to Python impossible to miss.

DEC-RUST-DEGRADE-SENTINEL-001.

WHY THIS EXISTS, STATED AS THE GAP IT CLOSES. The engagement census
(:mod:`brutex.observability.rust_census`, DEC-RUST-ENGAGEMENT-CENSUS-001 /
-002) answers two questions well and one question not at all:

* *Would* Rust answer on the next call?  ``census()`` — flag plus crate.
* *Did* Rust answer over a sampled workload?  ``prove()`` — the
  ``<name>.engaged`` first-answer event, joined per facade.
* Did Rust answer for the WHOLE RUN, every call, start to finish?
  **Neither of the above.**  ``census()`` reads intent, and ``prove()``
  reads a sample; a facade can be ENGAGED, answer once, PROVEN, and then
  autorevert on call 17 of a four-day sweep.  The census docstring names
  this blind spot itself.

This module closes it, and closes it from the opposite direction. Rather
than trying to observe every success — impossible, since the ``.engaged``
event fires **once per process** by design — it watches for the far
smaller, far louder set of events that mean *Rust stopped answering*.

THE INVERSION IS THE WHOLE POINT. Proving "Rust ran 33 billion times" is
not a thing a log can do. Proving "nothing ever fell back" is, because a
fall-back is required by the house pattern to announce itself exactly
once. So a run that COMPLETES with a CLEAN sentinel is a run in which no
enrolled facade degraded, autoreverted, mismatched, or refused a flag —
for its entire duration, not for a sample of it.

WHAT COUNTS AS A DEGRADE. :data:`DEGRADE_SUFFIXES` is the house
vocabulary, and it is a CONSTANT with a drift pin rather than a live scan:
the runtime path must stay O(1) per log record, and a test re-derives the
set from source on every CI run and fails if the two disagree. A new
suffix therefore cannot enter the codebase unnoticed.

WHY A SUFFIX ALONE IS NOT ENOUGH — a measured trap. The suffixes are NOT
private to Rust facades. ``auto_resume.disabled``, ``backup.drill.mismatch``,
``integrity.mismatch``, ``bhavcopy.mismatch``, ``universe.autopull.disabled``,
``server.heartbeat.disabled``, ``sweep_diffcheck.mismatch``,
``window_ledger_page.fallback`` and ``lake.stats_manifest.fallback`` all
end in a watched suffix and none of them is a Rust facade. A suffix-only
sentinel would have latched SUSPECT on an ordinary feature flag and been
switched off within a day — the exact fate of every alarm that cries wolf.

So an event fires the sentinel only when BOTH halves hold: the suffix is
in the vocabulary AND the namespace is a known Rust-facade namespace. The
namespace set is derived, never listed — see :func:`rust_namespaces`.

WHAT THIS DOES NOT DO (so the guarantee is not oversold).

* A CLEAN sentinel proves **no fall-back happened**. It does not prove
  **O(1)**. Those are independent properties with independent gates; see
  ``tests/bench/_o1_tiers.py`` and DEC-LAKE-SCAN-CHILD-DIRS-O1-001
  (#2261), where a fully-engaged Rust function on the hot path measured
  94.46x growth while every correctness test passed.
* It observes the loggers reachable from the CURRENT structlog processor
  chain. A module logger frozen against an OLDER chain (structlog's
  ``cache_logger_on_first_use``, documented at
  :func:`brutex.observability.logging._clear_structlog_first_use_cache`)
  bypasses it. The scope installs IN PLACE for exactly that reason —
  mutating the live list keeps every bound logger's reference valid — but
  a logger bound before the run configured logging is a real, narrow
  residual, named here rather than papered over.
* It is a WATCHER, not a policy. ``strict=True`` raises on the first
  degrade; the default observes and latches. Choosing which one a given
  entrypoint runs under is a separate decision, not this module's.
"""

from __future__ import annotations

import re
from collections.abc import Generator, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from brutex.models import BruteXError

#: Verdict tokens. Stable strings — surfaced in reports and asserted by
#: tests, so treat them as a wire vocabulary rather than prose.
CLEAN: Final[str] = "CLEAN"
DEGRADED: Final[str] = "DEGRADED"

#: The house degrade vocabulary, as word STEMS matched against an event's
#: suffix rather than exact suffix names.
#:
#: The stem rule is not a shortcut — it is the fix for a measured defect in
#: the first cut of this module. An exact-suffix set derived by grepping for
#: ``"…\.autoreverted"`` looked complete and was not: the house also emits
#: ``kernel_autoreverted`` (eight facades), ``locks_autoreverted``,
#: ``merge_autoreverted``, ``render_autoreverted``, ``shadow_mismatch`` and
#: ``call_fallback``. Twelve of eighty-nine real degrade events — thirteen
#: percent, including a wheel-REQUIRED seam's fall-back — would have been
#: invisible to a sentinel whose whole job is to see them. Stems generalise
#: over that prefix family automatically, so the next ``*_autoreverted`` is
#: covered the day it is written.
#:
#: Each stem, and the events it covers:
#:
#: * ``fallback`` — ``fallback``, ``call_fallback``. The crate was
#:   unavailable, stale or refused; the Python oracle served this call.
#: * ``revert`` — ``autoreverted`` and its four prefixed forms. The burn-in
#:   bit-compare disagreed, so the facade shipped the ORACLE value and
#:   latched onto Python for the rest of the process.
#: * ``disabl`` — ``rust_path_disabled``, ``disabled``, ``degraded``'s
#:   siblings. The Rust path was turned off.
#: * ``mismatch`` — ``mismatch``, ``shadow_mismatch``. A differential
#:   check disagreed.
#: * ``unrecognis`` — ``flag_unrecognised``. The flag held junk, so the
#:   facade chose Python rather than guess.
#: * ``refused`` — ``kernel_result_refused``. The refuse-poison gate
#:   rejected the crate's answer and the oracle served instead.
#: * ``uncertified`` — ``exit_indices_uncertified``. The kernel's O(trades)
#:   fast path was refused and the O(N_bars) dict build served
#:   (DEC-SWEEP-EXIT-INDEX-FASTPATH-001) — a complexity degrade, which is
#:   exactly as worth knowing as a language one.
#: * ``degrad`` — ``degraded``.
#:
#: Completeness is not asserted here; it is PROVEN on every CI run by the
#: exhaustive classification pin in ``tests/unit/test_rust_sentinel.py``,
#: which enumerates every event on every facade namespace and fails when one
#: is neither matched here nor listed as benign. A new degrade wording
#: therefore cannot enter the codebase unclassified.
DEGRADE_STEMS: Final[tuple[str, ...]] = (
    "fallback",
    "revert",
    "disabl",
    "mismatch",
    "unrecognis",
    "refused",
    "uncertified",
    "degrad",
)

#: Events that carry a degrade STEM inside a Rust namespace and are still
#: NOT a Rust fall-back. Keyed on the FULL event, never the suffix, so an
#: exemption can never leak to another facade.
#:
#: The namespace test is normally enough to keep the stems honest — the
#: :func:`classify` docstring's own example is ``auto_resume.disabled``,
#: excluded because ``auto_resume`` is not a facade. That reasoning assumes
#: a namespace belongs to ONE facade, which held while every facade lived
#: in a module named for it.
#:
#: DEC-RUST-CENSUS-DISCOVERY-MARKER-001 broke the assumption in the good
#: direction. Repairing the census's discovery hole enrolled two EMBEDDED
#: facades — a facade living inside a domain module rather than owning its
#: own — and with them five genuine Rust degrades the sentinel had never
#: been able to see (``cvar_gate.fallback``,
#: ``cvar_gate.kernel_autoreverted``, ``cvar_gate.kernel_result_refused``,
#: ``greeks_enrich.rust_batch_autoreverted``,
#: ``greeks_enrich.rust_batch_flag_unrecognised``). An embedded facade
#: SHARES its namespace with the module's ordinary domain logging, so the
#: namespace alone can no longer separate the two.
#:
#: This set is the cost, and it is deliberately one entry rather than a
#: general rule: exempting by prefix would be speculative generality for a
#: single case, and every entry here is a decision that should be read.
NOT_A_RUST_DEGRADE: Final[frozenset[str]] = frozenset(
    {
        # The Greeks PROCESS POOL fell back to the sequential path because
        # the worker context would not pickle. Concurrency, not language:
        # the Rust batch facade runs identically in either mode, so calling
        # it a Rust degrade would abort a strict sweep for a reason that has
        # nothing to do with Rust. brutex/data/greeks_enrich.py.
        "greeks_enrich.parallel_disabled",
    }
)

#: Literal ``"<namespace>.engaged"`` occurrences in a source file.
#:
#: The ``.engaged`` first-answer event suffix is the house marker for "this
#: is a Rust facade", and :func:`rust_namespaces` uses exactly that to tell a
#: facade namespace from an ordinary one. Matching the QUOTED literal (rather
#: than the bare suffix) is the narrower test: it reads the namespace the
#: facade actually emits, so a mention of ``.engaged`` in prose can never
#: enrol a module.
_ENGAGED_LITERAL_RE: Final[re.Pattern[str]] = re.compile(r"""["']([a-z_0-9.]+)\.engaged["']""")

#: Marks a source file as belonging to the RUST facade layer: it names a
#: ``BRUTEX_*RUST*`` flag or imports one of the house crates.
_RUST_SOURCE_RE: Final[re.Pattern[str]] = re.compile(
    r"""BRUTEX_[A-Z0-9_]*RUST[A-Z0-9_]*"""
    r"""|import_module\(\s*["'](?:brutex_|fno_math|trading_core)"""
)

#: Cache for :func:`rust_namespaces`. The derivation reads the package
#: source tree, which is stable for a process lifetime.
_NAMESPACES: list[frozenset[str]] = []


class RustDegradedError(BruteXError):
    """A Rust facade fell back to Python while the sentinel ran strict."""


def _literal_namespaces(root: Path | None = None) -> frozenset[str]:
    """Namespaces with a literal ``"<ns>.engaged"`` emitter under *root*.

    Defaults to the ``brutex`` package. The parameter exists so the
    unreadable-source arm is reachable from a test with a real
    permission-denied file rather than waved past with a pragma.

    This half of the derivation catches the facades the census cannot see:
    a shared core that logs on a facade's behalf (``rust_parity_core``
    emits ``rust_parity.engaged`` for the whole ranking family), and a
    facade whose module name defeats the census's name pre-filter.

    It is also the discriminator that keeps the sentinel from crying wolf.
    A namespace qualifies ONLY by emitting the first-answer event, which
    ordinary feature flags never do — so ``auto_resume.disabled`` and its
    eight measured siblings are excluded by construction rather than by an
    exclusion list that would need maintaining.

    An unreadable file is SKIPPED rather than raised: a source we cannot
    read contributes no namespace, and letting one bad file blind the whole
    derivation would turn every degrade into a silent pass — the exact
    failure this module exists to prevent.

    THE RUST DISCRIMINATOR. Emitting ``.engaged`` marks a burn-in seam, not
    necessarily a RUST one: ``prebuild.frame_write`` (a Python fast-lane
    versus the verbatim legacy lane, DEC-PREBUILD-FRAME-WRITE-001) and
    ``power.keepawake`` use the identical shape. Reporting their degrades
    under a banner that says "Rust fell back to Python" would be a false
    statement, so a namespace qualifies only when its emitting source names
    a Rust flag or crate, OR the namespace itself says ``rust``. The second
    arm is what keeps ``rust_parity`` — the shared ranking-family core,
    which takes its crate as a parameter and so names none — while still
    excluding the two seams above. Both arms are rules; neither is a list.
    """
    base = Path(__file__).resolve().parent.parent if root is None else root
    found: set[str] = set()
    for path in sorted(base.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        names = _ENGAGED_LITERAL_RE.findall(source)
        if not names:
            continue
        rust_source = _RUST_SOURCE_RE.search(source) is not None
        found.update(name for name in names if rust_source or "rust" in name)
    return frozenset(found)


def rust_namespaces() -> frozenset[str]:
    """Every event namespace that belongs to a Rust facade. DERIVED, not listed.

    The union of two independent derivations, each of which has a blind
    spot the other covers — which is the reason both are used rather than
    whichever one looked sufficient first:

    * The CENSUS leaves. :func:`~brutex.observability.rust_census.census`
      discovers facades by importing them and checking the house
      signature, so it sees the f-string delegators (the config trio
      ``paths_rust`` / ``dotenv_rust`` / ``backup_env_rust``, whose events
      are rendered by ``ConfigRustSeam`` and appear as no literal
      anywhere) and the wheel-REQUIRED seams that emit no ``.engaged``
      event at all.
    * The literal ``.engaged`` emitters (:func:`_literal_namespaces`).
      This sees the shared cores and the facades the census's module-name
      pre-filter skips.

    Neither alone is complete; the union is the honest answer, and it is
    pinned in the tests against both derivations so a regression in either
    is visible.
    """
    if _NAMESPACES:
        return _NAMESPACES[0]
    from brutex.observability.rust_census import census

    leaves = {row.module.rsplit(".", 1)[-1] for row in census()}
    resolved = frozenset(leaves) | _literal_namespaces()
    _NAMESPACES.append(resolved)
    return resolved


def classify(event: str, namespaces: frozenset[str]) -> str | None:
    """Return the degrade suffix *event* carries, or ``None``.

    Pure and O(1): one string partition, one set lookup, and a fixed
    eight-stem scan over a short suffix — no regular expression and no
    growth with the number of facades. This runs for every record a
    multi-day sweep emits, so the cost has to be a constant.

    Both halves must hold, and the namespace half is checked FIRST because
    it is the cheap one and it rejects the common case. ``lake_rust_render
    .fallback`` fires (Rust namespace, degrade stem); ``auto_resume
    .disabled`` does not (degrade stem, ordinary namespace);
    ``lake_rust_render.engaged`` does not (Rust namespace, no stem).

    A third check sits between them for the EMBEDDED facades — a facade
    living inside a domain module, whose namespace therefore carries the
    module's ordinary logging too. :data:`NOT_A_RUST_DEGRADE` names the
    full events that pass both halves and are still not a Rust fall-back;
    it is a frozenset lookup, so the O(1) shape is unchanged.
    """
    namespace, separator, suffix = event.rpartition(".")
    if not separator or namespace not in namespaces:
        return None
    if event in NOT_A_RUST_DEGRADE:
        return None
    return suffix if any(stem in suffix for stem in DEGRADE_STEMS) else None


@dataclass(slots=True)
class Degrade:
    """One observed fall-back, kept whole so a report can name it."""

    event: str
    suffix: str
    detail: MutableMapping[str, Any]


@dataclass(slots=True)
class RustSentinel:
    """A structlog processor that watches for Rust facades giving up.

    Installed at the FRONT of the live processor chain so it sees an event
    before any renderer consumes it, and it never modifies the event — the
    processor contract is honoured by returning ``event_dict`` unchanged,
    so logging behaves identically whether the sentinel is installed or not.
    """

    strict: bool = False
    namespaces: frozenset[str] = field(default_factory=rust_namespaces)
    degrades: list[Degrade] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        """``CLEAN`` until something falls back; ``DEGRADED`` forever after."""
        return DEGRADED if self.degrades else CLEAN

    def __call__(
        self, _logger: object, _method: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        """Observe one record. Never mutates it; raises only under ``strict``."""
        event = event_dict.get("event")
        if isinstance(event, str):
            suffix = classify(event, self.namespaces)
            if suffix is not None:
                self.degrades.append(Degrade(event=event, suffix=suffix, detail=dict(event_dict)))
                if self.strict:
                    raise RustDegradedError(
                        f"Rust facade fell back to Python: {event} "
                        f"({event_dict.get('reason', 'no reason given')})"
                    )
        return event_dict

    def report(self) -> str:
        """A human-readable verdict, listing every degrade in the order seen."""
        if not self.degrades:
            return f"{CLEAN}: no enrolled Rust facade fell back to Python."
        lines = [f"{DEGRADED}: {len(self.degrades)} fall-back(s) to Python."]
        lines.extend(f"  {d.event}  reason={d.detail.get('reason', '-')}" for d in self.degrades)
        return "\n".join(lines)


@contextmanager
def sentinel_scope(*, strict: bool = False) -> Generator[RustSentinel]:
    """Install a :class:`RustSentinel` for the duration of the block.

    Mutates the LIVE processors list in place rather than calling
    ``structlog.configure`` with a new one. That is not a shortcut: under
    ``cache_logger_on_first_use=True`` a bound logger holds a reference to
    the list it was assembled against, so replacing the list would leave
    every already-bound module logger emitting past the sentinel. In-place
    insertion is the same technique ``structlog.testing.capture_logs``
    uses, and for the same reason.

    Removal is by identity in a ``finally``, so a raise inside the block —
    including the sentinel's own ``strict`` raise — still un-installs.
    """
    import structlog

    sentinel = RustSentinel(strict=strict)
    processors = structlog.get_config()["processors"]
    processors.insert(0, sentinel)
    try:
        yield sentinel
    finally:
        for index, processor in enumerate(processors):
            if processor is sentinel:
                del processors[index]
                break
