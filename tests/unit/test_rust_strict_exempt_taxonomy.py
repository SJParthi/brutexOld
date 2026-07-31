"""Repo-wide taxonomy pin over the strict-Rust CLASS-C exemption sets.

DEC-RUST-STRICT-NO-FALLBACK-001 (:mod:`brutex.config.rust_strict`) converts
every silent Python fall-back into a :class:`~brutex.config.rust_strict.
RustFallbackRefused`. A facade that legitimately hands SOME inputs to Python
declares a ``_STRICT_EXEMPT_REASONS`` frozenset — the doctrine's **CLASS C**:
*"the input is outside the crate's envelope, Python was always the intended
handler"*.

THE GAP THIS FILE CLOSES (hostile review, MEDIUM). Nothing pinned that a
CLASS-C exempt set never accidentally swallows a CLASS-A or CLASS-B token. A
facade whose exempt set grew ``wheel_missing`` would silently STOP refusing an
absent wheel — the exact fall-back the DEC exists to forbid — and the suite
would stay green: the exemption is consulted before the refusal, so the seam's
own strict tests keep passing (they exercise a DIFFERENT reason), and no
existing test reads the constant at all.

Mechanism — DISCOVERY, never a hardcoded roster (the ``test_all_brutex_
doctests`` ``pkgutil`` sweep idiom, narrowed to a ``rglob`` + text pre-filter
so only the ~16 candidate modules are imported): walk ``brutex/**/*.py``, keep
the files whose TEXT names the constant, import them, and read the attribute
off the module. A facade added next quarter is pinned the day it lands; a
hardcoded list would pin only today's fifteen. The pre-filter is a filter, not
the oracle — ``brutex/config/rust_strict.py`` names ``_STRICT_UNRESOLVED_
REASONS`` in its own docstring without defining it, and is dropped by the
``getattr`` miss, not by an allowlist.

Import safety: none of the discovered facades hard-imports its crate at module
scope (each resolves the wheel lazily behind its own guard), so this walk runs
wheel-free. Verified by the walk itself — an import failure here is a hard
collection error, which is the honest signal.

Env hygiene: this file NEVER reads, sets or unsets ``BRUTEX_RUST_STRICT``. The
assertions are static — the constants are read, no facade's serving path is
called — so the shipped strict default is irrelevant to them, and the
suite-wide belt in ``tests/conftest.py`` (``_isolate_rust_strict_env`` + its
``trylast`` backstop, whose HONEST LIMIT docstring records the two CI shards a
leaked kill switch already turned red) has nothing to undo after this module.

Honest envelope (Golden Rule #10)
---------------------------------
* The token cross-check (:func:`test_never_exempt_tokens_are_emitted_by_a_
  facade`) is a TEXT scan for the quoted literal, not an AST emission proof: a
  token named only in a comment would satisfy it. It exists to stop the
  never-exempt roster below from drifting into fiction (asserting against
  tokens no facade produces), which a text scan settles; proving each token
  reaches a ``refuse_if_strict`` call is the seams' own behavioural tests.
* ``_STRICT_UNRESOLVED_REASONS`` deliberately gets a WEAKER pin — see
  :func:`test_unresolved_sets_never_overlap_their_own_exempt_set`.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Final

import brutex

#: The installed package root — resolved off the imported package (the
#: ``test_dashboard_threadpool_pin`` idiom) so the walk is CWD-independent.
_BRUTEX_ROOT: Final = Path(brutex.__file__).resolve().parent

#: The doctrine module itself. EXCLUDED from the emitter cross-check below: it
#: names every never-exempt token in its own scope-note docstring, so counting
#: it would make that pin self-satisfying (it would pass against a repo whose
#: facades emit none of them).
_DOCTRINE: Final = _BRUTEX_ROOT / "config" / "rust_strict.py"

#: Tokens that can NEVER be CLASS C, with the reason the failure message
#: prints. CLASS A = "Rust is absent or broke"; CLASS B = "Rust answered
#: wrongly". Both are exactly what strict mode exists to refuse — neither is
#: ever "a legitimate input the crate was never meant to take".
_NEVER_EXEMPT_WHY: Final[dict[str, str]] = {
    "wheel_missing": (
        "CLASS A — the Rust wheel is not installed. Exempting it re-enables "
        "the whole-facade Python fall-back the DEC forbids"
    ),
    "wheel_stale": (
        "CLASS A — the installed wheel predates the crate symbol, so the Rust "
        "arm is unreachable. Rebuild the wheel; never exempt it"
    ),
    "kernel_error": (
        "CLASS A — the kernel ran and faulted. A quiet Python re-run hides a real Rust defect"
    ),
    "crate_unavailable": (
        "CLASS A — the crate could not be imported. Same fall-back as a missing wheel, one guard up"
    ),
    "crate_stale": (
        "CLASS A — the crate API handshake failed (a symbol the facade needs "
        "is absent). The Rust arm is unreachable"
    ),
    "burn_in_mismatch": (
        "CLASS B — Rust DISAGREED with the Python oracle. Serving Python "
        "papers over a WRONG Rust answer, which is worse than refusing"
    ),
    "kernel_disabled": (
        "CLASS B — a prior burn-in mismatch latched this path OFF. Exempting "
        "it makes the autorevert a permanent silent Python fall-back"
    ),
}

#: The set form of the roster above.
_NEVER_EXEMPT: Final[frozenset[str]] = frozenset(_NEVER_EXEMPT_WHY)

#: MEASURED 2026-07-31 by running this file's own discovery walk: fifteen
#: modules define ``_STRICT_EXEMPT_REASONS`` (calendar_rust, detectors_rust,
#: fno_symbol_rust, lake_rust_stats, window_ledger_rust, rust_enum,
#: rust_trade_sim, exec_rust, rust_stats, models_rust, observability_rust,
#: optcost_rust, cvar_gate, rust_ranking, combo_rust). Asserted as a FLOOR,
#: never an equality: a new wired facade must not fail this pin, but deleting
#: the discovery mechanism (or breaking the text pre-filter) must.
_MEASURED_EXEMPT_FACADES: Final = 15

#: MEASURED 2026-07-31, same walk: one module declares
#: ``_STRICT_EXEMPT_REASON_PREFIXES`` (``exec_rust.py``, ``("envelope:",)``).
_MEASURED_PREFIX_FACADES: Final = 1

#: MEASURED 2026-07-31: SIX unresolved tokens repo-wide — rust_stage1a
#: ``{kernel_refused}`` (1), cvar_gate ``{kernel_error}`` (1), nightly_rust's
#: four ``*_error`` tokens (4), combo_rust deliberately EMPTY (0). This is the
#: exact residual the doctrine module's scope note names ("six tokens total"),
#: asserted as a CEILING — the honest close is a crate change that splits each
#: overloaded ``Err`` into a fault token and an ``*_input_beyond_crate`` token,
#: so this number may only ever SHRINK. A new unresolved token is a NEW
#: unclassified fall-back and must be a deliberate edit here plus a ledger
#: note, never a silent addition.
_MEASURED_UNRESOLVED_TOKEN_CEILING: Final = 6


def _module_name(path: Path) -> str:
    """Dotted module name for a ``brutex/`` source *path*.

    Plain join, no ``__init__.py`` special case: the constants are a
    FACADE-module convention and no package initialiser carries one (a
    defensive branch here would be an unexecutable line under the repo's
    100% line+branch gate).
    """
    relative = path.relative_to(_BRUTEX_ROOT).with_suffix("")
    return ".".join((brutex.__name__, *relative.parts))


def _discover(constant: str) -> dict[str, object]:
    """Every ``brutex`` module defining *constant*, mapped to its raw value.

    Text pre-filter → import → ``getattr``. The ``getattr`` miss is what drops
    a module that merely NAMES the constant in prose (the doctrine module), so
    the walk needs no exclusion list.
    """
    found: dict[str, object] = {}
    for path in sorted(_BRUTEX_ROOT.rglob("*.py")):
        if constant not in path.read_text(encoding="utf-8"):
            continue
        module = importlib.import_module(_module_name(path))
        value: object = getattr(module, constant, None)
        if value is None:
            continue
        found[module.__name__] = value
    return found


def _tokens(module: str, constant: str, raw: object) -> frozenset[str]:
    """Validate and normalise a discovered constant into a token set.

    Shape is asserted HERE (on a genuinely ``object``-typed value) rather than
    trusted: a facade that declared its exemptions as a bare ``str`` would
    otherwise be read character-by-character and silently exempt ``"w"``,
    ``"h"``, ``"e"`` … — a set-membership test on a string is the classic
    quiet way this pin could go vacuous.
    """
    assert isinstance(raw, (frozenset, tuple)), (
        f"{module}.{constant} must be a frozenset (or tuple, for the prefix "
        f"form) so membership tests are per-token; got {type(raw).__name__!r}. "
        "A bare str here would make every SUBSTRING an exemption."
    )
    non_str = sorted(repr(token) for token in raw if not isinstance(token, str))
    assert not non_str, f"{module}.{constant} holds non-str token(s): {', '.join(non_str)}"
    return frozenset(token for token in raw if isinstance(token, str))


_EXEMPT_RAW: Final[dict[str, object]] = _discover("_STRICT_EXEMPT_REASONS")
_PREFIX_RAW: Final[dict[str, object]] = _discover("_STRICT_EXEMPT_REASON_PREFIXES")
_UNRESOLVED_RAW: Final[dict[str, object]] = _discover("_STRICT_UNRESOLVED_REASONS")


def test_exempt_discovery_is_not_vacuous() -> None:
    """The walk finds at least the facades that exist today, with real sets.

    Tooth 3 of the gap fix: without this, deleting the discovery mechanism
    (or breaking the text pre-filter, or renaming the constant repo-wide)
    would turn every assertion below into a green no-op over an empty dict —
    the exact fake-green the ``test_all_brutex_doctests`` example floor exists
    to prevent, one constant over.
    """
    assert len(_EXEMPT_RAW) >= _MEASURED_EXEMPT_FACADES, (
        f"discovered only {len(_EXEMPT_RAW)} facade(s) defining "
        f"_STRICT_EXEMPT_REASONS ({sorted(_EXEMPT_RAW)}), below the "
        f"{_MEASURED_EXEMPT_FACADES} MEASURED 2026-07-31. The discovery walk "
        "is broken or the constant was renamed — a smaller roster silently "
        "un-pins the facades it dropped."
    )
    assert len(_PREFIX_RAW) >= _MEASURED_PREFIX_FACADES, (
        f"discovered {len(_PREFIX_RAW)} facade(s) defining "
        f"_STRICT_EXEMPT_REASON_PREFIXES, below the "
        f"{_MEASURED_PREFIX_FACADES} MEASURED 2026-07-31 (exec_rust.py)."
    )
    for module, raw in sorted(_EXEMPT_RAW.items()):
        assert _tokens(module, "_STRICT_EXEMPT_REASONS", raw) is not None


def test_exempt_sets_never_hold_a_class_a_or_class_b_reason() -> None:
    """No CLASS-C exempt set may contain a never-exempt token.

    THE pin. Adding e.g. ``wheel_missing`` to any facade's exempt set stops
    strict mode refusing an absent wheel — a silent re-enable of the Python
    fall-back — and fails here, naming the module, the token, and why that
    token can never be CLASS C.
    """
    for module, raw in sorted(_EXEMPT_RAW.items()):
        tokens = _tokens(module, "_STRICT_EXEMPT_REASONS", raw)
        forbidden = sorted(tokens & _NEVER_EXEMPT)
        assert not forbidden, (
            f"{module}._STRICT_EXEMPT_REASONS exempts CLASS-A/CLASS-B "
            f"token(s) from strict mode: "
            + "; ".join(f"{token!r} — {_NEVER_EXEMPT_WHY[token]}" for token in forbidden)
            + ". DEC-RUST-STRICT-NO-FALLBACK-001: CLASS C is ONLY 'the input "
            "is outside the crate's envelope, Python was always the intended "
            "handler'. Widen the crate (or fix the Rust arm) — never exempt "
            "the reason."
        )


def test_exempt_prefixes_never_swallow_a_never_exempt_reason() -> None:
    """The prefix form of the exemption cannot capture a never-exempt token.

    ``exec_rust.py`` exempts by PREFIX as well as by exact token
    (``_strict_exempt`` ors ``reason.startswith(...)``), so an over-broad
    prefix — ``"kernel"``, or a bare ``""`` — would swallow CLASS-A/B reasons
    without any exact token ever appearing in a set. Same hole, second door.
    """
    for module, raw in sorted(_PREFIX_RAW.items()):
        prefixes = _tokens(module, "_STRICT_EXEMPT_REASON_PREFIXES", raw)
        swallowed = sorted(
            (prefix, token)
            for prefix in prefixes
            for token in _NEVER_EXEMPT
            if token.startswith(prefix)
        )
        assert not swallowed, (
            f"{module}._STRICT_EXEMPT_REASON_PREFIXES swallows never-exempt "
            f"reason(s): "
            + "; ".join(
                f"prefix {prefix!r} captures {token!r} — {_NEVER_EXEMPT_WHY[token]}"
                for prefix, token in swallowed
            )
        )


def test_unresolved_sets_never_overlap_their_own_exempt_set() -> None:
    """A token is either classified CLASS C or unclassifiable — never both.

    THE WEAKER PIN, deliberately. ``_STRICT_UNRESOLVED_REASONS`` CANNOT take
    the never-exempt disjointness above: ``brutex/ranking/cvar_gate.py``
    declares ``{"kernel_error"}`` there ON PURPOSE — its single
    ``except (ValueError, TypeError, OverflowError)`` catches BOTH the
    kernel's structural ``Err`` arms (CLASS A/B) and PyO3's ``Vec<i64>``
    extraction failing at the FFI edge before any kernel code runs (CLASS C),
    so the token is genuinely two classes at once and the DEC's posture is to
    REPORT it, not guess. Asserting disjointness there would fail shipped,
    correct, documented code.

    What IS pinnable, and what this asserts instead:

    1. **Per-module exclusivity** — a token in a module's exempt set must not
       also sit in its unresolved set. Those are contradictory claims
       ("provably a legitimate Python-serve" vs "class unresolvable"), and a
       token in both would make the doctrine's own census meaningless.
    2. **A census CEILING** — the residual may only shrink. The doctrine names
       exactly six unresolved tokens repo-wide; the honest close is a crate
       change splitting each overloaded ``Err``. A NEW unresolved token is a
       new unclassified fall-back, which the DEC requires to be *named*, so it
       must be a deliberate edit to the constant below.
    """
    census: dict[str, list[str]] = {}
    for module, raw in sorted(_UNRESOLVED_RAW.items()):
        unresolved = _tokens(module, "_STRICT_UNRESOLVED_REASONS", raw)
        census[module] = sorted(unresolved)
        exempt = _tokens(module, "_STRICT_EXEMPT_REASONS", _EXEMPT_RAW.get(module, frozenset()))
        both = sorted(unresolved & exempt)
        assert not both, (
            f"{module} declares {both} in BOTH _STRICT_EXEMPT_REASONS "
            "(provably a legitimate Python-serve) and "
            "_STRICT_UNRESOLVED_REASONS (class not resolvable from the crate "
            "source). Those claims contradict; pick one and cite the crate."
        )
    total = sum(len(tokens) for tokens in census.values())
    assert total <= _MEASURED_UNRESOLVED_TOKEN_CEILING, (
        f"{total} unresolved reason token(s) repo-wide ({census}), above the "
        f"{_MEASURED_UNRESOLVED_TOKEN_CEILING} MEASURED 2026-07-31 and named "
        "in brutex/config/rust_strict.py's scope note. A new unresolved token "
        "is a NEW unclassified Python fall-back — classify it against the "
        "crate source, or raise this ceiling deliberately with a ledger entry."
    )


def test_never_exempt_tokens_are_emitted_by_a_facade() -> None:
    """The never-exempt roster names real reason tokens, not invented ones.

    Without this, the roster could drift into fiction — a renamed token would
    leave every assertion above trivially satisfiable while the REAL token
    went unpinned. Honest limit (see the module docstring): this is a text
    scan for the quoted literal, so a mention in a comment counts; it pins the
    roster against drift, not each token's path to ``refuse_if_strict``.
    """
    sources = {
        str(path.relative_to(_BRUTEX_ROOT)): path.read_text(encoding="utf-8")
        for path in sorted(_BRUTEX_ROOT.rglob("*.py"))
        if path != _DOCTRINE
    }
    for token in sorted(_NEVER_EXEMPT):
        emitters = sorted(
            rel for rel, src in sources.items() if f'"{token}' in src or f"'{token}" in src
        )
        assert emitters, (
            f"never-exempt token {token!r} appears in NO brutex/ facade "
            "source (the doctrine module itself is excluded, since it names "
            "every token in prose). Either the facades renamed it — update "
            "_NEVER_EXEMPT_WHY so the taxonomy pin keeps biting — or it was "
            "never a real reason token and does not belong in the roster."
        )
