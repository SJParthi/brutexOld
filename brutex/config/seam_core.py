"""The ONE shared engaged-call core for the milestone-3 config Rust seams.

DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943), milestone 3. The three
plain-config-leaf facades (:mod:`brutex.config.paths_rust` /
:mod:`brutex.config.dotenv_rust` / :mod:`brutex.config.backup_env_rust`)
share EVERY fail-closed layer of the milestone-1/2 template — the flag
tristate, the latch, the import/API-surface handshake, the per-law
burn-in bit-compare + ship-the-oracle autorevert, the refuse-poison gate,
the narrow degrade set, the pyo3 panic belt, the RAW escape-set re-raise,
the warn-once registry and the test reset seam. Milestones 1 and 2
carried that machinery as per-facade copies with documented reasons
(independent seams, mid-review surface stability); a THIRD, FOURTH and
FIFTH copy would be exactly the drift hazard the mirror-pins exist to
catch (CLAUDE.md §11 COMMON + the jscpd zero-duplication gate), so
milestone 3 hoists the machinery HERE as :class:`ConfigRustSeam` — one
instance per facade, each with its OWN flag, law budgets, latch and
crate-API handshake. The milestone-1/2 facades are deliberately NOT
refolded onto this core mid-PR (their consolidation is a candidate
follow-up, never forced here).

Honesty banner (ADR-0020, mandatory verbatim): expected payoff ≈ 0 —
conversion is for uniformity, not speed.

LEAF discipline: stdlib + structlog ONLY at module scope (AST-pinned in
``tests/unit/test_config_seam_core.py``) — these seams sit under
``brutex.config.paths`` / ``dotenv``, which bootstrap the CLI before any
wheel-bearing package may load. The inline ``_TRUTHY``/``_FALSY`` flag
vocabulary is the feed_rust/money_rust local-twin precedent,
mirror-pinned against :mod:`brutex.money_rust`'s sets (which are
themselves pinned against ``sweep_lookahead``'s — one transitive chain,
no import edge). No version guard (the money_rust reasoning verbatim):
the crate side is pure std-Rust locked to probed CPython laws — there is
no wheel↔crate lockstep to attest; the burn-in + the parity-lane
differentials are the drift teeth.

SECRET SAFETY (the dotenv seam's law): a seam constructed with
``redact_values=True`` NEVER places a crossed/returned VALUE in a log —
burn-in mismatches and refuse-poison warns carry types/counts only, and
(r2 m4) the degrade/panic arms carry the exception TYPE only (an
exception repr can embed a crossed value — e.g. a PyO3 conversion
TypeError echoing its argument), making the contract structural.
``.env`` values can be operator secrets (Telegram tokens, S3 keys); the
dotenv module's own "never log values" law extends through its seam.

``MemoryError`` / ``KeyboardInterrupt`` / ``SystemExit`` re-raise RAW
(the ``errors.py`` escape-set doctrine — an OOM must stay a global
signal, never be laundered into a fallback).
"""

from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING, Final, cast

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

_LOG = structlog.get_logger(__name__)

#: Engaged calls compared against the Python oracle PER LAW before crate
#: values ship un-oracled (the #1531/#1532 burn-in law).
BURN_IN_SAMPLES: Final[int] = 16

#: Inline flag vocabulary (LEAF discipline — module docstring; the
#: mirror-pin test asserts byte-equality with ``money_rust``'s sets).
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: The engaged-INFO memo key (never a warn reason).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: The narrow degrade-to-oracle catch set. ``ValueError`` is the crate's
#: declared envelope refusal; ``RuntimeError`` its declared panic
#: mapping; ``TypeError``/``OverflowError`` are PyO3 argument-conversion
#: failures; ``AttributeError`` the broken-crate-internals belt.
#: ``MemoryError`` is DELIBERATELY absent (module docstring).
_DEGRADE_ERRORS: Final[tuple[type[Exception], ...]] = (
    ValueError,
    RuntimeError,
    TypeError,
    OverflowError,
    AttributeError,
)


def panic_belt_errors() -> tuple[type[BaseException], ...]:
    """The narrow FFI panic-belt catch set — the money_rust/hwenv twin.

    ``pyo3_runtime.PanicException`` is the one ``BaseException`` subclass
    a pyo3 boundary can leak; the repo convention (CodeQL
    ``py/catch-base-exception``) forbids a non-re-raising literal
    ``except BaseException``, so the class is resolved GUARDEDLY by name
    and folded beside ``Exception``. The crate's own ``catch_unwind``
    maps panics to ``RuntimeError`` (the degrade set), so this belt is
    defense-in-depth over a broken boundary.
    """
    belt: list[type[BaseException]] = [Exception]
    try:
        module = importlib.import_module("pyo3_runtime")
    except ImportError:
        return tuple(belt)
    panic = getattr(module, "PanicException", None)
    if isinstance(panic, type) and issubclass(panic, BaseException) and panic not in belt:
        belt.append(panic)
    return tuple(belt)


class ConfigRustSeam:
    """One config seam's fail-closed engaged-call state machine.

    Layers (every path serves the PYTHON-law value):

    1. the caller-side flag gate (:meth:`enabled` — default OFF; junk
       values loud-OFF once per process);
    2. the facade-owned ELIGIBILITY gates (checked BEFORE :meth:`adopt`
       is ever called — an ineligible input never touches the crate);
    3. the import/API-surface guard (:meth:`_resolve_crate` — absent
       wheel latches ``crate_unavailable``; a wheel missing a callable
       latches ``crate_stale``);
    4. the refuse-poison gate (the facade's *validate* — a wrong-typed or
       shape-law-violating crate return is refused with one WARN);
    5. the per-law burn-in self-check (the first :data:`BURN_IN_SAMPLES`
       engaged calls PER LAW compare both arms; ANY difference ships the
       ORACLE value and autoreverts the Rust path for the process);
    6. the degrade set + the pyo3 panic belt (a crate raise serves the
       oracle with one WARN / a latch, never a new crash class).
    """

    def __init__(
        self,
        *,
        seam: str,
        env_var: str,
        crate_api: tuple[str, ...],
        laws: tuple[str, ...],
        redact_values: bool = False,
        default_on: bool = False,
        strict_facade: str | None = None,
    ) -> None:
        self.seam = seam
        self.env_var = env_var
        self.crate_api = crate_api
        self.laws = laws
        self.redact_values = redact_values
        self.default_on = default_on
        #: OPT-IN strict-mode wiring (DEC-RUST-STRICT-NO-FALLBACK-001). When a
        #: facade passes its module path here, every fall-back this seam
        #: ANNOUNCES (``_warn_once``/``_disable``) refuses under
        #: ``BRUTEX_RUST_STRICT`` instead of degrading. Left ``None`` the seam
        #: behaves exactly as before — so a consumer that has NOT been
        #: classified (currently ``brutex.config.backup_env_rust``) is
        #: byte-identical, never silently swept into strict mode.
        self.strict_facade = strict_facade
        self._disabled_reason: str | None = None
        self._burn_in: dict[str, int] = dict.fromkeys(laws, BURN_IN_SAMPLES)
        self._memo: set[str] = set()
        #: Resolved-crate memo (``None`` = not yet resolved) — the
        #: ``money_rust`` pattern (DEC-MONEY-RUST-WRAPPER-COST-001), which
        #: measured the crate resolve at 911 ns/call because
        #: ``importlib.import_module`` costs ~574 ns even on a
        #: ``sys.modules`` HIT and the symbol handshake the rest.
        #: :meth:`adopt` pays it on EVERY engaged call.
        #:
        #: PER-INSTANCE, not module-level: each seam carries its own
        #: :attr:`crate_api` handshake and its own latch, so a memo shared
        #: across instances would let one seam's successful handshake
        #: satisfy another's — the crate module is the same object, but the
        #: SURFACE CHECK is not, and skipping it is exactly the stale-wheel
        #: hole the handshake exists to close.
        #:
        #: THE MODULE is memoized, never a bound callable: :meth:`adopt`
        #: does ``getattr(crate, crate_name)`` per call, so a test that
        #: monkeypatches a crate ATTRIBUTE is still seen — a callable memo
        #: would silently miss it.
        #:
        #: SUCCESS ONLY. A FAILED resolve is never cached: both failure arms
        #: go through :meth:`_disable`, which under strict mode REFUSES
        #: *before* it latches, so caching a failure would convert "every
        #: call re-attempts and re-refuses, loudly" into the silently-latched
        #: steady state strict mode exists to forbid.
        #:
        #: Cleared by :meth:`reset_for_tests` — a test that swaps
        #: ``sys.modules["brutex_core"]`` must reset, or it is served the
        #: crate resolved before its swap.
        self._crate_memo: ModuleType | None = None

    # ── Flag + latch + reset ────────────────────────────────────────────

    def enabled(self) -> bool:
        """Resolve the seam's env flag (per-seam default, junk loud-OFF).

        Truthy → ``True``; falsy → ``False`` (silent explicit OFF — the
        kill switch); unset → the seam's ``default_on`` off the raw probe
        alone (the r1 ABSENT fast path); blank → ``default_on``; any OTHER
        non-empty value → ``False`` with one ``flag_unrecognised`` WARN
        per process (the junk arm never picks the Rust path). Read per
        call so spawn workers resolve their own inherited environment
        (the PR-1 posture; caching would break that contract). The three
        config seams construct with ``default_on=True`` per
        DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive 2026-07-23).
        """
        raw = os.environ.get(self.env_var)
        if raw is None:
            return self.default_on
        stripped = raw.strip().lower()
        if stripped in _TRUTHY:
            return True
        if stripped in _FALSY:
            return False
        if stripped:
            self._warn_once("flag_unrecognised", value=repr(stripped))
            return False
        return self.default_on

    def disabled(self) -> bool:
        """``True`` iff a guard/autorevert latched the Rust path OFF."""
        return self._disabled_reason is not None

    @property
    def disabled_reason(self) -> str | None:
        """The latch reason (``None`` = live) — test/observability read."""
        return self._disabled_reason

    def reset_for_tests(self, *, remaining: int = BURN_IN_SAMPLES) -> None:
        """Reset latch + per-law burn-in budgets + warn memo (tests ONLY).

        ``remaining=0`` puts the seam in the post-burn-in steady state so
        the adopted value is the crate's with no oracle in the path (the
        parity legs depend on that). Never called from production paths.
        """
        self._disabled_reason = None
        for law in self._burn_in:
            self._burn_in[law] = remaining
        self._memo.clear()
        self._crate_memo = None

    def burn_in_remaining(self, law: str) -> int:
        """The live burn-in budget for *law* (test/observability read)."""
        return self._burn_in[law]

    # ── Observability ───────────────────────────────────────────────────

    def _refuse_if_strict(self, reason: str, **context: object) -> None:
        """Strict-mode gate for the two announcement choke points.

        No-op unless the OWNING facade opted in via ``strict_facade`` (see
        :meth:`__init__`). Imported FUNCTION-LOCALLY on purpose: this module
        is a LEAF whose module-scope imports are AST-pinned to stdlib +
        structlog (``tests/unit/test_config_seam_core.py::
        test_leaf_import_discipline_ast_pin``), and ``rust_strict`` itself
        follows the same in-function-import convention.
        """
        if self.strict_facade is None:
            return
        from brutex.config.rust_strict import refuse_if_strict

        refuse_if_strict(self.strict_facade, reason, seam=self.seam, **context)

    def _warn_once(self, reason: str, **context: object) -> None:
        """Refuse under strict mode; else log ``<seam>_rust.fallback`` warn-once.

        Every reason funnelled here is CLASS A (the crate is absent, stale or
        raised: ``flag_unrecognised``, ``{law}_error``) or CLASS B (the crate
        answered but the answer was refused: ``{law}_result_refused``) — both
        refuse under ``BRUTEX_RUST_STRICT``. There is NO class-C reason at
        this choke point: the facades' input-envelope gates (paths'
        ``_eligible_lake_root``/``_eligible_volume_parent``, dotenv's
        ``_eligible_text``) return the Python law WITHOUT announcing, so an
        out-of-envelope input still reaches Python under strict mode exactly
        as it does today. Strict-OFF is byte-identical to the prior behaviour.
        """
        self._refuse_if_strict(reason, **context)
        if reason in self._memo:
            return
        self._memo.add(reason)
        _LOG.warning(f"{self.seam}_rust.fallback", reason=reason, **context)

    def _disable(self, reason: str, *, error: bool = False, **context: object) -> None:
        """Refuse under strict mode; else latch the Rust path OFF for the process.

        Reasons here are CLASS A (``crate_unavailable``/``crate_stale``/
        ``crate_panic``) or CLASS B (``burn_in_mismatch_{law}`` — the crate
        DISAGREED with the oracle). The refusal fires BEFORE the latch is
        written, so under strict mode the latch can never be set and the
        latched early-return in :meth:`adopt` is structurally unreachable —
        i.e. wiring these two functions covers every announced fall-back.
        Strict-OFF is byte-identical to the prior behaviour.
        """
        self._refuse_if_strict(reason, error=error, **context)
        if self._disabled_reason is not None:
            return
        self._disabled_reason = reason
        if error:
            _LOG.error(f"{self.seam}_rust.autoreverted", reason=reason, **context)
        else:
            _LOG.warning(f"{self.seam}_rust.rust_path_disabled", reason=reason, **context)

    def _log_engaged_once(self, crate: ModuleType) -> None:
        """Log ``<seam>_rust.engaged`` INFO once per process."""
        if _ENGAGED_KEY in self._memo:
            return
        self._memo.add(_ENGAGED_KEY)
        _LOG.info(f"{self.seam}_rust.engaged", crate_version=getattr(crate, "__version__", None))

    # ── Crate resolution (layer 3) ──────────────────────────────────────

    def _resolve_crate(self) -> ModuleType | None:
        """Import guard + SYMBOL handshake: ``brutex_core`` or latch+None.

        An absent wheel (``ImportError``) latches ``crate_unavailable``.
        A wheel that imports but lacks any callable :attr:`crate_api`
        member is a pre-milestone-3 (or foreign/broken) install — latch
        ``crate_stale``. Fail-closed: never an ``AttributeError`` out of
        this seam.

        A SUCCESSFUL resolve is memoized in :attr:`_crate_memo` (the
        reasoning, and why failures are deliberately NOT cached, is on that
        attribute). Cleared by :meth:`reset_for_tests`.
        """
        if self._crate_memo is not None:
            return self._crate_memo
        try:
            crate = importlib.import_module("brutex_core")
        except ImportError as exc:
            self._disable(
                "crate_unavailable",
                error_detail=repr(exc),
                remedy="pip install rust/brutex-core (maturin build; see rust/brutex-core/README.md)",
            )
            return None
        for name in self.crate_api:
            if not callable(getattr(crate, name, None)):
                self._disable(
                    "crate_stale",
                    error_detail=f"brutex_core.{name} missing or not callable — "
                    f"the installed wheel predates the {self.seam} seam (or is foreign/broken)",
                    remedy="rebuild + reinstall rust/brutex-core (see rust/brutex-core/README.md)",
                )
                return None
        self._crate_memo = crate
        return crate

    # ── The engaged-call core (layers 3-6) ──────────────────────────────

    def adopt[T](
        self,
        law: str,
        crate_name: str,
        arguments: tuple[object, ...],
        validate: Callable[[object], bool],
        oracle: Callable[[], T],
    ) -> T | None:
        """Run layers 3-6 for one engaged call; return the crate value to
        ADOPT, or ``None`` (= serve the Python oracle).

        *oracle* computes the pure Python-law result in the CRATE's
        comparable form — only invoked while the law's burn-in budget is
        live. A burn-in mismatch autoreverts and returns ``None``: the
        caller recomputes the (pure, deterministic) oracle — one
        redundant compute on the one-shot autorevert path, never on the
        steady state. The ``cast`` on the adopted value is sound by
        construction: every call site's *validate* pins the exact runtime
        shape its *oracle* returns, so a type-punned crate return can
        never reach the cast.
        """
        crate = self._resolve_crate()
        if crate is None:
            return None
        try:
            rust_value: object = getattr(crate, crate_name)(*arguments)
        except _DEGRADE_ERRORS as exc:
            # r2 m4: on a secret-bearing seam the exception repr may
            # embed a crossed VALUE (a PyO3 conversion TypeError echoes
            # its argument) — log the TYPE only there, making the
            # "no crossed VALUE in logs" contract structural.
            if self.redact_values:
                self._warn_once(f"{law}_error", error_type=type(exc).__name__)
            else:
                self._warn_once(f"{law}_error", error_detail=repr(exc))
            return None
        except (KeyboardInterrupt, SystemExit, MemoryError):
            # The escape set (the errors.py doctrine): operator interrupt
            # / interpreter shutdown / OOM stay global — never a silent
            # fallback. Sits ABOVE the panic belt so its `Exception`
            # member can never swallow a MemoryError.
            raise
        except panic_belt_errors() as exc:
            # A panic escaping the crate's catch_unwind boundary (or any
            # out-of-degrade-set Exception) marks a broken FFI contract —
            # latch OFF and serve the oracle, never a new crash class
            # into a config-resolution path. Value-redacting seams log
            # the exception TYPE only (r2 m4 — the m4 contract above).
            if self.redact_values:
                self._disable("crate_panic", error_type=type(exc).__name__)
            else:
                self._disable("crate_panic", error_detail=repr(exc))
            return None
        if not validate(rust_value):
            # Refuse-poison gate: a wrong-typed or shape-law-violating
            # return is a crate BUG — never served. Structurally
            # unreachable through the real crate; guards a stale/foreign
            # install. Value-REDACTED on a secret-bearing seam.
            if self.redact_values:
                self._warn_once(f"{law}_result_refused", result_type=type(rust_value).__name__)
            else:
                self._warn_once(
                    f"{law}_result_refused",
                    result_type=type(rust_value).__name__,
                    value_head=repr(rust_value)[:48],
                )
            return None
        self._log_engaged_once(crate)
        if self._burn_in[law] > 0:
            oracle_value = oracle()
            if rust_value != oracle_value:
                if self.redact_values:
                    self._disable(
                        f"burn_in_mismatch_{law}",
                        error=True,
                        detail="values redacted (secret-bearing seam)",
                        rust_type=type(rust_value).__name__,
                    )
                else:
                    self._disable(
                        f"burn_in_mismatch_{law}",
                        error=True,
                        rust_value=repr(rust_value),
                        python_value=repr(oracle_value),
                    )
                return None
            self._burn_in[law] -= 1
            if self._burn_in[law] == 0:
                _LOG.info(f"{self.seam}_rust.burn_in_complete", law=law, samples=BURN_IN_SAMPLES)
        return cast("T", rust_value)


__all__ = [
    "BURN_IN_SAMPLES",
    "ConfigRustSeam",
    "panic_belt_errors",
]
