"""Runtime census of which Rust facades are ACTUALLY engaged.

DEC-RUST-ENGAGEMENT-CENSUS-001.

THE PROBLEM THIS SOLVES. Every Rust facade in this repo follows the same
shape: an env flag, a lazily-imported crate, and a Python oracle to fall
back on. When the crate is missing or stale the facade DEGRADES — it warns
**once per process** and then runs Python silently for the rest of the run.

That once-per-process latch is correct behaviour (a per-call warn would
drown the logs) but it has a consequence nobody can argue away: **absence
of a degrade message in a log proves nothing**. A log capture that starts
after the latch fired looks identical to a log from a fully-Rust run. A
census over 328,717 production log lines could therefore only *prove*
engagement for 2 of 34 facades; the other 32 were INDETERMINATE — not
broken, just unknowable from that evidence.

"Is it really running Rust?" is not answerable by reading logs, and it is
not answerable by reading source (the source says what SHOULD happen). It
is only answerable by ASKING THE PROCESS. That is what this module does.

WHAT IT REPORTS. For each discovered facade, three independently-checked
facts and the verdict they force:

===========  ==========  =============  ====================================
flag         crate       verdict        meaning
===========  ==========  =============  ====================================
ON           present     ENGAGED        Rust runs.
ON           MISSING     **DEGRADED**   Python runs. The dangerous cell —
                                        it is the one that looks fine.
OFF          any         OFF            Python runs, by explicit choice.
n/a          required    REQUIRED       No oracle exists; a missing crate
                                        is a loud typed refusal, not a
                                        silent fallback.
===========  ==========  =============  ====================================

DISCOVERY IS STRUCTURAL, NOT A LIST. A hardcoded facade table is a lie
waiting to happen: the next facade added would be absent from it and the
census would report a clean bill of health while ignoring it. Instead
:func:`discover_facades` walks the package for modules exposing the house
facade signature — a ``<NAME>_ENV`` string constant AND a matching
``<name>_enabled()`` predicate — so a new facade is enrolled the moment it
is written, and one that DROPS the signature is reported as a shape
change rather than silently vanishing.

WHAT THIS DOES NOT DO (stated so the guarantee is not oversold).

* It reports what the facade WOULD do on its next call, from the flag and
  the crate's importability. It does not intercept an actual call, so a
  facade whose burn-in has already AUTOREVERTED (a bit-compare mismatch
  latched it onto the oracle for this process) still reports ENGAGED here.
  That is a genuine blind spot, and it is narrow: an autorevert emits a
  loud ERROR that is *not* rate-limited, so unlike a silent degrade it
  cannot pass unnoticed.
* It says nothing about COMPLEXITY. A facade can be fully ENGAGED and
  still be O(n) — see DEC-LAKE-SCAN-CHILD-DIRS-O1-001 (#2261), where a
  Rust function on the hot path was measured at 94.46x growth. Engagement
  and complexity are independent properties with independent gates; this
  module is the engagement half only.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import pkgutil
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Final

#: Verdict tokens. Stable strings — surfaced in reports and asserted by
#: tests, so treat them as a wire vocabulary rather than prose.
ENGAGED: Final[str] = "ENGAGED"
DEGRADED: Final[str] = "DEGRADED"
OFF: Final[str] = "OFF"
REQUIRED: Final[str] = "REQUIRED"
UNKNOWN: Final[str] = "UNKNOWN"

#: The house facade signature. A module qualifies when it exports BOTH a
#: ``<NAME>_ENV`` string constant and a zero-argument ``<name>_enabled()``
#: predicate. Both halves are required: the constant alone appears in
#: consumers that merely reference a flag, and the predicate alone appears
#: in wheel-REQUIRED seams that carry no flag at all (handled separately
#: via :data:`_REQUIRED_SEAM_MARKERS`).
_ENV_SUFFIX: Final[str] = "_ENV"
_ENABLED_SUFFIX: Final[str] = "_enabled"

#: Source markers identifying a wheel-REQUIRED seam — one whose Python
#: oracle was DELETED, so a missing crate raises rather than degrades.
#: These are the gold standard and are reported separately so they are
#: never miscounted as "flag OFF".
#: A NAMED-CLASS rule rather than a per-facade allowlist. The house
#: retired-axis law gives every wheel-required seam a refusal class whose
#: name says so — ``…UnavailableError`` when the wheel is absent and
#: ``…FlagRemovedError`` when a retired flag is set. Matching the SHAPE
#: means a facade retired tomorrow is classified correctly without editing
#: this file; the earlier literal list silently mis-reported
#: ``pull_state_rust`` as ``UNKNOWN``.
#:
#: Deliberately NARROW. A first cut also matched ``…KernelError`` and
#: ``Stale…WheelError``, which promoted two CONDITIONAL seams to REQUIRED:
#: ``rust_trade_sim`` refuses only on the >=64-bit wide-mask path and
#: degrades on u64, and ``exec_rust`` refuses only on a STALE wheel while an
#: ABSENT one degrades. Both would have inflated the count of seams whose
#: completion proves Rust ran. REQUIRED here means unconditionally required.
_REQUIRED_CLASS_RE: Final[re.Pattern[str]] = re.compile(r"\b\w*(?:Unavailable|FlagRemoved)Error\b")

_REQUIRED_SEAM_MARKERS: Final[tuple[str, ...]] = (
    "require_feed_kernel",
    "_require_crate",
    "require_materialize_flags",
)

#: Package root scanned for facades.
_PACKAGE: Final[str] = "brutex"


def _source_of(module: ModuleType) -> str:
    """Return a module's source, or ``""`` when it cannot be read.

    A compiled or zip-imported module has no readable source. That costs
    the source-derived facts (crate name, required-seam marker) but must
    never abort the census, so the caller sees an empty string and the row
    falls through to the honest ``UNKNOWN``.
    """
    try:
        return inspect.getsource(module)
    except (OSError, TypeError):
        return ""


def _is_required_seam(source: str) -> bool:
    """Report whether a facade REFUSES rather than degrades on a bad wheel.

    Two independent signals, either sufficient: an explicit ``require_*``
    helper call, or a refusal class named by the retired-axis convention
    (see :data:`_REQUIRED_CLASS_RE`). The distinction is load-bearing —
    a required seam that cannot find its crate raises, so "the run
    completed" is itself proof the Rust path executed. A degrading seam
    offers no such proof, which is exactly the gap this census exists to
    surface.
    """
    if any(marker in source for marker in _REQUIRED_SEAM_MARKERS):
        return True
    return _REQUIRED_CLASS_RE.search(source) is not None


def _crate_names() -> frozenset[str]:
    """Every crate this repo builds, read from ``rust/*/Cargo.toml``.

    Derived rather than hardcoded so a new crate is recognised without a
    census edit — the same anti-list discipline as facade discovery.
    Falls back to an empty set when the Rust tree is absent (an installed
    wheel-only deployment), which degrades the census to flag-only facts
    rather than crashing.
    """
    root = Path(__file__).resolve().parents[2] / "rust"
    if not root.is_dir():
        return frozenset()
    names: set[str] = set()
    for manifest in sorted(root.glob("*/Cargo.toml")):
        try:
            text = manifest.read_text(encoding="utf-8")
        except OSError:
            continue
        lib = re.search(r"^\[lib\][^\[]*?name\s*=\s*\"([^\"]+)\"", text, re.S | re.M)
        if lib is not None:
            names.add(lib.group(1))
            continue
        pkg = re.search(r"^\[package\][^\[]*?name\s*=\s*\"([^\"]+)\"", text, re.S | re.M)
        if pkg is not None:
            names.add(pkg.group(1).replace("-", "_"))
    return frozenset(names)


@dataclass(frozen=True, slots=True)
class FacadeStatus:
    """One facade's independently-checked engagement facts."""

    module: str
    env_var: str | None
    flag_on: bool | None
    crate: str | None
    crate_present: bool | None
    verdict: str
    detail: str = ""

    @property
    def is_rust_running(self) -> bool:
        """True only when Rust genuinely executes on the next call."""
        return self.verdict in (ENGAGED, REQUIRED)


# ──────────────────────────────────────────────────────────────────────
# Call-level proof — DEC-RUST-ENGAGEMENT-CENSUS-002
# ──────────────────────────────────────────────────────────────────────
#
# The flag-level verdicts above answer "is Rust configured to run, and does
# its crate import?". That is strictly weaker than "did Rust answer", and
# the gap is not academic: every facade's remaining guards fire at CALL
# time, inside the work function, not at flag resolution. The sharpest
# example is the lake family's polars lockstep guard, which lives inside
# ``render_parquet_bytes`` (brutex/data/lake_rust_render.py): it reads the
# crate's pinned polars version, compares it against the installed wheel's,
# and on any difference disables the facade with ``polars_version_mismatch``
# and returns the Python render.
#
# So a wheel/crate version drift (a one-line dependency bump moving
# ``polars==`` without moving the crate rev) silently routes the WHOLE
# ``BRUTEX_LAKE_RUST_*`` family back to the Python oracle while every
# flag-level verdict still reads ENGAGED. Measured, not reasoned: patching
# ``polars.__version__`` before import leaves :func:`census` reporting
# ``ENGAGED=32 DEGRADED=0``.
#
# The fix is to stop inferring and start observing. Each facade emits
# exactly one ``<name>.engaged`` structlog event the first time its crate
# genuinely answers, and a degrade/disable event otherwise. Those events
# are per-process warn-once, which is precisely why they are useless for
# reading a long-running log — and precisely why they are reliable when
# captured from a COLD start around a known workload: at that point every
# first decision is still unlatched, so the event stream is a complete
# record of who answered.
#
# :func:`engaged_events` captures that stream. A facade whose event fires
# is PROVEN — Rust ran, measured on this machine, this build, this wheel
# set. A facade that stays silent is CONFIGURED: the flags are right and
# the crate imports, but nothing in the workload called it, so this run
# says nothing either way about it. Keeping those two apart is the whole
# point; collapsing them is how "Rust is on" becomes a claim nobody
# checked.

PROVEN: Final[str] = "PROVEN"
CONFIGURED: Final[str] = "CONFIGURED"

#: The uniform per-facade first-answer event suffix.
_ENGAGED_EVENT_SUFFIX: Final[str] = ".engaged"

#: Literal ``"<name>.engaged"`` occurrences in a facade's own source.
_ENGAGED_LITERAL_RE: Final[re.Pattern[str]] = re.compile(r"""["']([a-z_0-9]+)\.engaged["']""")


def _accepted_events(module_name: str, source: str) -> frozenset[str]:
    """Event names that count as proof for this facade, DERIVED from source.

    The obvious join -- "the module's leaf name is the event name" -- is
    WRONG for a third of the census, and measurably so: 10 of 32 ENGAGED
    facades emit under a different name (``hardware_envelope_rust`` emits
    ``hwenv_rust.engaged``, ``compute_rust`` emits ``metrics_rust_fold``,
    ``rust_enum`` emits ``sweep_rust_enum``, and so on). Joining on the leaf
    would have reported CONFIGURED for facades that genuinely answered.

    Two rules, in order, each derived rather than listed:

    * The literals in the facade's OWN source. This is exact and covers
      every direct emitter.
    * Failing that, the leaf name. This is not a guess: the config trio
      (``paths_rust`` / ``dotenv_rust`` / ``backup_env_rust``) holds no
      literal because ``ConfigRustSeam`` renders ``f"{seam}_rust.engaged"``
      on their behalf -- which evaluates to exactly the leaf.

    TWO facades stay structurally unprovable by this mechanism, named here
    rather than papered over, and BOTH fail in the conservative direction
    (they read CONFIGURED when Rust may in fact have answered -- never a
    false PROVEN):

    * ``ranking.rust_ranking`` delegates to ``ParityFamily``, whose event is
      the SHARED ``rust_parity.engaged``. Accepting a shared name would let
      one family's answer prove another's, so it is deliberately refused.
    * ``meta.meta_rust`` emits no ``.engaged`` event at all; it has a
      fallback, a flag and an autorevert event, but no first-answer one.
    """
    literals = frozenset(_ENGAGED_LITERAL_RE.findall(source))
    return literals or frozenset({module_name.rsplit(".", 1)[-1]})


def engaged_events(workload: Callable[[], object]) -> frozenset[str]:
    """Run *workload* and return the ``<name>`` of every facade that answered.

    Captures structlog centrally for the duration of the call, then keeps
    the events whose name ends in ``.engaged`` — the house convention for
    "the crate genuinely answered, first time this process". The capture is
    restored even when *workload* raises, and the exception propagates: a
    workload that blew up measured nothing, and silently returning a
    half-populated set would read as "these facades are not engaged" when
    the truth is "we never got that far".
    """
    import structlog
    from structlog.testing import LogCapture

    capture = LogCapture()
    saved = structlog.get_config()
    structlog.configure(processors=[capture])
    try:
        workload()
    finally:
        structlog.configure(**saved)
    return frozenset(
        event[: -len(_ENGAGED_EVENT_SUFFIX)]
        for entry in capture.entries
        if isinstance(event := entry.get("event"), str) and event.endswith(_ENGAGED_EVENT_SUFFIX)
    )


def _crate_for_source(source: str, crates: frozenset[str]) -> str | None:
    """Identify the crate a facade imports, by scanning its source.

    Deliberately source-scanning rather than import-triggering: a facade's
    crate import is lazy by design (so the dashboard boots wheel-free) and
    forcing it here would change the very state we are measuring.

    Two shapes are recognised, because the house pattern uses both:

    * ``importlib.import_module("brutex_lake")`` -- the DOMINANT shape.  The
      lazy resolve is the whole point of the facade, so the crate name is a
      string literal, not an import statement.  Scanning only for ``import
      <crate>`` reported ``UNKNOWN`` for 29 of 37 facades on the first run.
    * ``import fno_math`` / ``from trading_core import ...`` -- the few
      module-scope hard imports (a wheel-required seam has nothing to be
      lazy about).
    """
    for crate in sorted(crates, key=len, reverse=True):
        escaped = re.escape(crate)
        if re.search(rf"""import_module\(\s*["']{escaped}["']""", source):
            return crate
        if re.search(rf"\b(?:import|from)\s+{escaped}\b", source):
            return crate
    return None


def _delegated_crate(source: str, crates: frozenset[str]) -> str | None:
    """Resolve the crate for a facade that delegates its resolve to a helper.

    Several facades hold no crate literal of their own because the lazy
    resolve lives in a SHARED seam helper -- ``config.seam_core.ConfigRustSeam``
    for the config trio, ``engine.rust_parity_core.ParityFamily`` for the
    ranking family.  Scanning only the facade's own source reported ``UNKNOWN``
    for those.

    One hop, and derived rather than hardcoded: read the source of each
    ``brutex.*`` module the facade imports directly and look for the crate
    literal there.  One hop is deliberate -- it covers the delegation shape
    the codebase actually uses, and refuses to chase a transitive graph whose
    answer would no longer be "the crate THIS facade routes to".
    """
    for match in re.finditer(r"^from\s+(brutex\.[\w.]+)\s+import\b", source, re.MULTILINE):
        try:
            helper = importlib.import_module(match.group(1))
            helper_source = inspect.getsource(helper)
        except (ImportError, OSError, TypeError):
            continue
        crate = _crate_for_source(helper_source, crates)
        if crate is not None:
            return crate
    return None


def _classify(
    *,
    env_var: str | None,
    flag_on: bool | None,
    crate: str | None,
    crate_present: bool | None,
    required_seam: bool,
) -> tuple[str, str]:
    """Derive the verdict from the three checked facts."""
    if required_seam and crate is not None:
        if crate_present:
            return REQUIRED, "wheel-required seam; crate present"
        return (
            DEGRADED,
            f"wheel-REQUIRED seam but {crate} is NOT importable — this raises at call time",
        )
    if crate is None:
        return UNKNOWN, "no crate import found in source — facade shape may have changed"
    if flag_on is None:
        return UNKNOWN, "flag state could not be resolved"
    if not flag_on:
        return OFF, f"{env_var} resolves falsy — Python oracle runs by choice"
    if crate_present:
        return ENGAGED, f"{env_var} on and {crate} importable"
    return DEGRADED, f"{env_var} is ON but {crate} is NOT importable — Python runs SILENTLY"


def discover_facades() -> list[str]:
    """Return every module exposing the house Rust-facade signature.

    Structural discovery: a module qualifies when it exports a
    ``<NAME>_ENV`` string AND a ``<name>_enabled()`` callable, or when it
    carries a wheel-REQUIRED marker. Import failures are skipped rather
    than raised so one broken optional module cannot blind the whole
    census (the skip is visible as an absent row, and the count pins in
    the tests catch a collapse).
    """
    package = importlib.import_module(_PACKAGE)
    found: list[str] = []
    for info in pkgutil.walk_packages(package.__path__, prefix=f"{_PACKAGE}."):
        name = info.name
        # Cheap pre-filter on the module NAME before paying an import:
        # every facade in this repo carries "rust" or "seam" in its path.
        tail = name.rsplit(".", 1)[-1]
        if "rust" not in tail and "seam" not in tail:
            continue
        # The census is not a facade. It matches its own name pre-filter and
        # carries the required-seam MARKER STRINGS as data, so without this
        # it audits itself and reports a phantom REQUIRED row.
        # ``__name__`` is ``"__main__"`` under ``python -m``, so compare
        # against the SPEC name, which stays the dotted path either way.
        if __spec__ is not None and name == __spec__.name:
            continue
        try:
            module = importlib.import_module(name)
        except Exception:  # noqa: S112 - a broken optional module must not blind the census
            continue
        has_env = any(
            attr.endswith(_ENV_SUFFIX) and isinstance(getattr(module, attr, None), str)
            for attr in dir(module)
        )
        has_enabled = any(
            attr.endswith(_ENABLED_SUFFIX) and callable(getattr(module, attr, None))
            for attr in dir(module)
        )
        source = _source_of(module)
        required_seam = _is_required_seam(source)
        if (has_env and has_enabled) or required_seam:
            found.append(name)
    return sorted(found)


def census(modules: list[str] | None = None) -> list[FacadeStatus]:
    """Ask every facade what it WOULD do on its next call."""
    crates = _crate_names()
    names = modules if modules is not None else discover_facades()
    rows: list[FacadeStatus] = []
    for name in names:
        try:
            module = importlib.import_module(name)
        except Exception as exc:
            rows.append(
                FacadeStatus(
                    module=name,
                    env_var=None,
                    flag_on=None,
                    crate=None,
                    crate_present=None,
                    verdict=UNKNOWN,
                    detail=f"import failed: {type(exc).__name__}",
                )
            )
            continue

        env_var: str | None = None
        for attr in sorted(dir(module)):
            if attr.endswith(_ENV_SUFFIX):
                value = getattr(module, attr, None)
                if isinstance(value, str) and value.startswith("BRUTEX_"):
                    env_var = value
                    break

        # Resolve EVERY zero-arg ``*_enabled`` predicate and AND them, rather
        # than taking the first alphabetically. Three reasons, each hit in
        # practice on the first run:
        #   * ``detectors_rust`` owns THREE independent flags -- one sampled
        #     answer would speak for laws it does not govern.
        #   * ``dir()`` also surfaces IMPORTED predicates, so "first
        #     alphabetically" could resolve a neighbour's flag.
        #   * ``pull_state_rust`` exposes an ARG-TAKING ``…_enabled(raw)``;
        #     breaking on it left the whole row unresolved. Skipping it lets
        #     the wheel-required marker classify the row correctly instead.
        # A facade with no resolvable predicate stays ``None`` -- an honest
        # "could not resolve", never an assumed True.
        resolved: list[bool] = []
        for attr in sorted(dir(module)):
            if not attr.endswith(_ENABLED_SUFFIX):
                continue
            fn = getattr(module, attr, None)
            if not callable(fn):
                continue
            try:
                resolved.append(bool(fn()))
            except Exception:  # noqa: S112 - an unresolvable flag is a fact, not a crash
                continue
        flag_on: bool | None = all(resolved) if resolved else None

        source = _source_of(module)
        crate = _crate_for_source(source, crates) or _delegated_crate(source, crates)
        required_seam = _is_required_seam(source)
        crate_present: bool | None = None
        if crate is not None:
            try:
                crate_present = importlib.util.find_spec(crate) is not None
            except (ImportError, ValueError):
                crate_present = False

        verdict, detail = _classify(
            env_var=env_var,
            flag_on=flag_on,
            crate=crate,
            crate_present=crate_present,
            required_seam=required_seam,
        )
        rows.append(
            FacadeStatus(
                module=name,
                env_var=env_var,
                flag_on=flag_on,
                crate=crate,
                crate_present=crate_present,
                verdict=verdict,
                detail=detail,
            )
        )
    return rows


def summarize(rows: list[FacadeStatus]) -> dict[str, int]:
    """Count rows per verdict."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.verdict] = counts.get(row.verdict, 0) + 1
    return counts


def render_table(rows: list[FacadeStatus]) -> str:
    """Human-readable census, widest-first so the verdict column lines up."""
    if not rows:
        return "NO FACADES DISCOVERED — the discovery signature has changed."
    mod_w = max(len(r.module) for r in rows)
    env_w = max(len(r.env_var or "-") for r in rows)
    crate_w = max(len(r.crate or "-") for r in rows)
    out = [
        f"{'FACADE':<{mod_w}}  {'FLAG':<{env_w}}  {'CRATE':<{crate_w}}  VERDICT",
        f"{'-' * mod_w}  {'-' * env_w}  {'-' * crate_w}  {'-' * 8}",
    ]
    for row in sorted(rows, key=lambda r: (r.verdict, r.module)):
        out.append(
            f"{row.module:<{mod_w}}  {row.env_var or '-':<{env_w}}  "
            f"{row.crate or '-':<{crate_w}}  {row.verdict}"
        )
    counts = summarize(rows)
    out.append("")
    out.append("  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    degraded = [r for r in rows if r.verdict == DEGRADED]
    if degraded:
        out.append("")
        out.append("DEGRADED — Python is running where Rust was expected:")
        for row in degraded:
            out.append(f"  {row.module}: {row.detail}")
    return "\n".join(out)


#: Verdicts that fail ``--strict``. A facade is only allowed through when
#: it is provably going to take the Rust path: ENGAGED (flag on, crate
#: importable) or REQUIRED (no Python arm exists at all).
_STRICT_FAILING: Final[frozenset[str]] = frozenset({DEGRADED, OFF, UNKNOWN})


def strict_failures(rows: list[FacadeStatus]) -> list[FacadeStatus]:
    """Return the facades that would NOT take the Rust path.

    This is the preflight half of the guarantee. Running it immediately
    before a sweep converts "Rust is the default" -- a statement about
    configuration -- into "Rust will run" -- a statement about this
    process, on this machine, with these wheels installed.

    What it proves: every facade's flag is on and its crate imports here
    and now, so no facade can silently take a Python arm for want of a
    wheel or a flag.

    What it does NOT prove, stated plainly because the difference matters:

    * **Complexity.** A facade can be Rust and still be O(n) -- exactly
      the defect measured in ``child_dirs`` (94.46x at 100x the siblings),
      which every correctness test passed. Rust-ness and O(1) are
      independent properties; the bench tier is what gates the second.
    * **Burn-in autorevert.** A crate that imports cleanly but disagrees
      with its Python oracle is dropped mid-run. That path is not silent
      -- it logs an ERROR that is not rate-limited -- but it happens
      after this gate has already passed.
    * **Call-time guards.** Every remaining guard fires inside the work
      function, not at flag resolution -- the lake family's polars
      lockstep check most of all. Use :func:`prove` for that half;
      ENGAGED here means "configured and importable", never "answered".
    """
    return [row for row in rows if row.verdict in _STRICT_FAILING]


def prove(
    workload: Callable[[], object], rows: list[FacadeStatus] | None = None
) -> list[FacadeStatus]:
    """Re-verdict *rows* from what actually happened while *workload* ran.

    Upgrades each ENGAGED row to :data:`PROVEN` when that facade's
    ``.engaged`` event fired during the workload, and downgrades it to
    :data:`CONFIGURED` when it stayed silent. REQUIRED, DEGRADED, OFF and
    UNKNOWN rows pass through untouched -- they were already decided by
    stronger evidence than a workload trace, and a silent REQUIRED facade
    is not a finding (it has no Python arm to fall back to).

    The distinction is the honest one: PROVEN is a measurement, CONFIGURED
    is an expectation nothing exercised. A run that reports CONFIGURED has
    not found a defect -- it has found an untested seam, which is a
    different and much less alarming thing, and conflating the two would
    manufacture either false alarm or false comfort depending on which way
    you rounded.
    """
    base = census() if rows is None else rows
    answered = engaged_events(workload)
    out: list[FacadeStatus] = []
    for row in base:
        if row.verdict != ENGAGED:
            out.append(row)
            continue
        # ``sys.modules``, never a fresh import: an ENGAGED row was imported
        # by the census that produced it, and importing here would change
        # the very state being measured (the same reason the crate resolver
        # source-scans instead of import-triggering).
        module = sys.modules.get(row.module)
        source = _source_of(module) if module is not None else ""
        accepted = _accepted_events(row.module, source)
        fired = sorted(accepted & answered)
        out.append(
            replace(
                row,
                verdict=PROVEN if fired else CONFIGURED,
                detail=(
                    f"{fired[0]}.engaged observed" if fired else "flag on, crate imports, uncalled"
                ),
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    """Print the census.

    Exit 1 if any facade is DEGRADED; under ``--strict`` also fail on OFF
    or UNKNOWN, so a clean exit means every facade will take the Rust path.
    """
    args = sys.argv[1:] if argv is None else argv
    strict = "--strict" in args
    rows = census()
    print(render_table(rows))

    failures = strict_failures(rows) if strict else [r for r in rows if r.verdict == DEGRADED]
    if not failures:
        return 0
    print("\nSTRICT FAILURES:" if strict else "\nDEGRADED:")
    for row in failures:
        print(f"  {row.module}  [{row.verdict}]  {row.detail}")
    return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
