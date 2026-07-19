"""The per-feed universe SCOPE law — DEC-UNIVERSE-INDEX-ONLY-001 (MIGRATION.md #1934).

The runtime-dynamic 4-index universe cut: ONE per-feed map (``feed →
FeedScope``) consulted by every admission predicate and every read-time
enumeration filter. Today the map is ``{groww: index4, dhan: index4}``
(:data:`DEFAULT_FEED_SCOPES` — the coordinator-relayed 2026-07-17 per-feed
map, directive (B) paraphrase, never operator wording); a future GDFL
feed binds wider by CONFIG (``BRUTEX_FEED_SCOPE=gdfl=full``) plus its own
adapter REGISTRATION, never by re-architecture (directive B).

## The two consumer laws (design §2.4 — recorded here per the design doc)

* **Admission + vendor-action enumeration** bind the **ACTING feed's** scope
  (:func:`resolve_feed_scope`, default :data:`DEFAULT_FEED`) — the bare kwarg
  default is fail-closed NARROW under the default config. Any future
  second-adapter DEC must thread its own feed explicitly AND add its vendor
  token to :data:`DEFAULT_FEED_SCOPES` (without it a bare-token env never
  covers the new feed — it lives on the fail-closed default behind the
  ``feed_scope.active_feed_fail_closed_default`` WARN).
* **Display / census / lake-walk surfaces** bind :func:`resolve_read_scope` —
  the UNION over every **ACTIVE** feed's scope, threaded explicitly at each
  surface's named production chokepoint (the read seams themselves default
  ``None`` = unfiltered legacy). Since the union ⊇ every active feed's scope,
  a display can never hide what a producer admits.

## ACTIVE feeds (design §2.5 — the registration seam)

ACTIVE = adapter-registered. Seeded ``{"groww"}`` statically (the feeds
registry's unconditional default row — boot-order independent; the registry is
instance-scoped per chain, so no module-level read of "its rows" exists) and
grown ONLY by :func:`note_feed_registered`, called from
``VendorProfileRegistry.register`` exactly where profile state LANDS (a
REFUSED registration never grows the set). Import direction: feeds → config
ONLY. Registration is the ONE code event that folds a feed into
:func:`resolve_read_scope` — the union memo keys on ``(env, active set)`` so
the fold takes effect on the NEXT resolve with no env movement and no restart;
the union stays process-scoped. ``dhan`` is scope-KNOWN (a
:data:`DEFAULT_FEED_SCOPES` key) but adapterless, hence NOT active.

## The env override — ``BRUTEX_FEED_SCOPE`` (design §2.3, the full grammar)

EITHER a bare token applying to all scope-known feeds (``index4`` | ``full``)
XOR a per-feed CSV of ``key=token`` pairs (``groww=index4,gdfl=full``). Blank
or whitespace-only ≡ UNSET (the house ``env_flag_tristate`` convention).
Refused loudly (:class:`FeedScopeError` naming the env var + fragment):
unknown token; empty entry / empty key / bare ``=``; a duplicate key after
normalization (never last-wins); mixing the bare form with ``key=`` pairs.
Non-ACTIVE keys are accepted + stored (how a GDFL binding is laid down
pre-adapter) with one ``feed_scope.unregistered_feed_binding`` WARN for a
scope-UNKNOWN key, EXCLUDED from the read union, and a WIDE binding on any
non-active key additionally warns ``feed_scope.inactive_feed_wide_binding`` —
so a typo'd / premature / adapterless key is fully inert on BOTH admission and
reads, AND warned. Junk fails loud at boot/entry via the §2.3 eager-validation
sites (:func:`validate_feed_scope_env`), never mid-pull. A mid-process ENV
flip is unsupported (resolution memoized per raw env string — the house
resolve-once posture); mid-process widening by REGISTRATION is supported BY
DESIGN (the union memo + :func:`scope_revision` move with the active set).
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, cast

import structlog

from brutex.models import Exchange

if TYPE_CHECKING:  # pragma: no cover — typing-only import (no runtime edge)
    from collections.abc import Mapping

_LOG = structlog.get_logger(__name__)

#: The narrow scope token: exactly the 4 ``V1_INDICES`` pairs.
SCOPE_INDEX4: Final[str] = "index4"

#: The unbounded scope token: the pre-#1934 legacy admission rules.
SCOPE_FULL: Final[str] = "full"

_KNOWN_TOKENS: Final[frozenset[str]] = frozenset({SCOPE_INDEX4, SCOPE_FULL})

#: Mirror of ``universe.V1_INDICES``' ``(exchange, underlying)`` pairs.
#: LITERALS deliberately — this leaf cannot import :mod:`brutex.universe`
#: (universe.py imports THIS leaf); drift is caught by the mirror-pin test
#: ``test_index4_underlyings_mirror_universe`` (tests/unit/test_feed_scope.py,
#: the ``lake_keys.stock_drift_fold ↔ universe.normalize_underlying_symbol``
#: mirror-pin idiom).
INDEX4_UNDERLYINGS: Final[frozenset[tuple[Exchange, str]]] = frozenset(
    {
        (Exchange.NSE, "NIFTY"),
        (Exchange.NSE, "BANKNIFTY"),
        (Exchange.BSE, "SENSEX"),
        (Exchange.NSE, "INDIAVIX"),
    }
)

#: The F&O-bearing subset (INDIAVIX has no chain — reference-only spot).
#: Mirror-pinned to the NON-reference ``V1_INDICES`` pairs by its OWN test
#: (``test_index4_fno_underlyings_mirror_non_reference_v1`` — design §2.1:
#: without its own pin a ``V1_INDICES`` edit would leave the admitted pin
#: green while this subset silently diverged). Deliberately NEVER derived
#: from the 3-symbol ENGINE-trio constant (directive E / prior-art C8): keying
#: any admission arm on the engine trio silently kills INDIAVIX admission —
#: the trio token is HARD-BANNED in this leaf (test_feed_scope.py).
INDEX4_FNO_UNDERLYINGS: Final[frozenset[tuple[Exchange, str]]] = frozenset(
    {
        (Exchange.NSE, "NIFTY"),
        (Exchange.NSE, "BANKNIFTY"),
        (Exchange.BSE, "SENSEX"),
    }
)


class FeedScopeError(ValueError):
    """A junk ``BRUTEX_FEED_SCOPE`` value — loud refusal naming var + fragment.

    Raised at boot/entry by the §2.3 eager-validation sites (exit-1 boot /
    clean 400), never mid-pull. Retrying without fixing the env is a config
    bug, not a transient.
    """


@dataclass(frozen=True, slots=True)
class FeedScope:
    """One feed's resolved universe scope (frozen — design §2.2).

    ``admitted_underlyings`` / ``fno_underlyings`` are ``None`` ⇔ unbounded
    (the ``full`` token); bounded scopes carry explicit
    ``(Exchange, symbol)`` sets. The read UNION (:func:`resolve_read_scope`)
    is also expressed as a :class:`FeedScope` with the reserved feed label
    :data:`READ_UNION_FEED`.
    """

    feed: str
    token: str
    admitted_underlyings: frozenset[tuple[Exchange, str]] | None
    fno_underlyings: frozenset[tuple[Exchange, str]] | None

    def admits_underlying(self, exchange: Exchange, symbol: str) -> bool:
        """True iff a SPOT underlying ``(exchange, symbol)`` is in scope."""
        if self.admitted_underlyings is None:
            return True
        return (exchange, symbol) in self.admitted_underlyings

    def admits_fno_underlying(self, exchange: Exchange, symbol: str) -> bool:
        """True iff derivative legs of ``(exchange, symbol)`` are in scope."""
        if self.fno_underlyings is None:
            return True
        return (exchange, symbol) in self.fno_underlyings

    def admits_stocks(self) -> bool:
        """True iff single-stock (EQ / NTM) admission is in scope at all."""
        return self.token == SCOPE_FULL

    def bounded(self) -> bool:
        """True iff this scope names an explicit admitted set (not ``full``)."""
        return self.admitted_underlyings is not None


#: The acting-feed default for admission surfaces (design §2.4).
DEFAULT_FEED: Final[str] = "groww"

#: The pre-laid scope DEFAULTS (the coordinator-relayed 2026-07-17 per-feed
#: map — directive (B), coordinator paraphrase, never operator wording).
#: Scope-KNOWN keys — NOT an activity claim: ``dhan`` has NO adapter in-tree
#: and is NOT an ACTIVE feed until one registers (§2.5).
DEFAULT_FEED_SCOPES: Final[Mapping[str, str]] = MappingProxyType(
    {"groww": SCOPE_INDEX4, "dhan": SCOPE_INDEX4}
)

#: The env override knob (design §2.3 — the full grammar in the module doc).
#: ``ENV_FEED_SCOPE`` is the canonical ``ENV_*``-pattern declaration the
#: ``scripts/ops/print_env_schema.py`` registry greps for
#: (DEC-ENV-REGISTRY-001, MIGRATION.md #244 — the config-package idiom:
#: ``ENV_LAKE_ROOT``/``ENV_RUNTIME_FILE``/``ENV_PORT_*``);
#: ``FEED_SCOPE_ENV`` is the name every consumer imports (kept — both name
#: the ONE string, never two knobs).
ENV_FEED_SCOPE: Final[str] = "BRUTEX_FEED_SCOPE"
FEED_SCOPE_ENV: Final[str] = ENV_FEED_SCOPE

#: The reserved feed label the read-union :class:`FeedScope` carries.
READ_UNION_FEED: Final[str] = "<read-union>"


@dataclass(frozen=True, slots=True)
class _ParsedEnv:
    """One parsed non-blank ``BRUTEX_FEED_SCOPE`` value (internal)."""

    #: The bare-token form's token, or ``None`` for the per-feed CSV form.
    bare_token: str | None
    #: The CSV form's normalized ``{feed_key: token}`` map (empty for bare).
    per_feed: Mapping[str, str]


_LOCK = threading.Lock()
#: ACTIVE feeds — seeded with the registry's unconditional default row (§2.5).
_ACTIVE_SEED: Final[frozenset[str]] = frozenset({DEFAULT_FEED})
_active: set[str] = set(_ACTIVE_SEED)
#: Memo: raw env string (``None`` = unset) → parsed form. Parse-once per string.
_parse_memo: dict[str | None, _ParsedEnv | None] = {}
#: The :func:`_parse_env` miss sentinel — ``None`` is a legitimate memo VALUE
#: (blank/unset parses to ``None``), so the single-op ``dict.get`` read below
#: needs a distinct miss marker.
_PARSE_MISS: Final[object] = object()
#: Memo: ``(feed, raw env string)`` → resolved :class:`FeedScope` (§2.3 memo law).
_feed_memo: dict[tuple[str, str | None], FeedScope] = {}
#: Memo: ``(raw env string, active snapshot)`` → the read union (§2.3 memo law —
#: keying on the active SNAPSHOT is what makes widen-by-registration reach a
#: primed memo with zero env movement).
_union_memo: dict[tuple[str | None, frozenset[str]], FeedScope] = {}
#: Warn-once registry, keyed ``(event, feed_key)`` — cleared by the test seam.
_warned: set[tuple[str, str]] = set()


def _refuse(raw: str, why: str) -> FeedScopeError:
    """Build the loud junk-env refusal naming the env var + value + fragment."""
    return FeedScopeError(
        f"{FEED_SCOPE_ENV}={raw!r} is invalid: {why} — expected a bare token "
        f"({SCOPE_INDEX4!r} | {SCOPE_FULL!r}) XOR a per-feed CSV of key=token "
        f"pairs (e.g. 'groww=index4,gdfl=full')"
    )


def _parse_env(raw: str | None) -> _ParsedEnv | None:
    """Parse *raw* per the §2.3 grammar — memoized; junk raises :class:`FeedScopeError`.

    ``None`` / blank / whitespace-only → ``None`` (≡ UNSET → the defaults
    map — the house blank-is-unset convention; the "empty entry" refusal
    applies only WITHIN a non-blank value).

    The memoized read is ONE GIL-atomic ``dict.get`` (never check-then-index):
    production memos are add-only, but the test seam :func:`_reset_for_tests`
    CLEARS them under ``_LOCK`` around every test — a daemon thread leaked
    from a prior test (an SWR revalidator resolving scope) could interleave a
    clear between a two-step ``in`` probe and the index and KeyError inside
    that daemon. One atomic op leaves no interleave point (flake-hardening
    only; no production impact — regression-pinned).
    """
    memoized = _parse_memo.get(raw, _PARSE_MISS)
    if memoized is not _PARSE_MISS:
        # The sentinel identity check narrows the value back to the memo's
        # own ``_ParsedEnv | None`` domain (dict.get widened it to object).
        return cast("_ParsedEnv | None", memoized)
    parsed = _parse_env_uncached(raw)
    with _LOCK:
        _parse_memo[raw] = parsed
    return parsed


def _parse_env_uncached(raw: str | None) -> _ParsedEnv | None:
    """The uncached §2.3 grammar parse (see :func:`_parse_env`)."""
    if raw is None or not raw.strip():
        return None
    entries = [entry.strip() for entry in raw.split(",")]
    if any(not entry for entry in entries):
        raise _refuse(raw, "empty entry in the CSV value")
    pair_flags = ["=" in entry for entry in entries]
    if not any(pair_flags):
        if len(entries) != 1:
            raise _refuse(raw, f"the bare-token form takes exactly ONE token, got {entries!r}")
        token = entries[0].lower()
        if token not in _KNOWN_TOKENS:
            raise _refuse(raw, f"unknown scope token {entries[0]!r}")
        return _ParsedEnv(bare_token=token, per_feed=MappingProxyType({}))
    if not all(pair_flags):
        raise _refuse(raw, "mixing the bare-token form with key=token pairs")
    per_feed: dict[str, str] = {}
    for entry in entries:
        if entry.count("=") != 1:
            raise _refuse(raw, f"malformed entry {entry!r} (exactly one '=' expected)")
        key_raw, _, token_raw = entry.partition("=")
        key = key_raw.strip().lower()
        token = token_raw.strip().lower()
        if not key or not token:
            raise _refuse(raw, f"empty key or token in entry {entry!r}")
        if token not in _KNOWN_TOKENS:
            raise _refuse(raw, f"unknown scope token {token_raw.strip()!r} for feed {key!r}")
        if key in per_feed:
            raise _refuse(raw, f"duplicate feed key {key!r} (never last-wins)")
        per_feed[key] = token
    return _ParsedEnv(bare_token=None, per_feed=MappingProxyType(per_feed))


def _warn_once(event: str, feed_key: str, **fields: object) -> None:
    """Emit *event* at WARNING once per ``(event, feed_key)`` per process.

    The ``feed`` field rides ``repr()`` (the #907 escaping discipline): the
    §2.3 grammar deliberately ACCEPTS arbitrary unregistered keys (how a
    GDFL binding is pre-laid), so an operator-typed control-char-bearing key
    would otherwise ride raw into a rendered log stream — the sibling
    ``_refuse`` path already interpolates ``{raw!r}``.
    """
    with _LOCK:
        if (event, feed_key) in _warned:
            return
        _warned.add((event, feed_key))
    _LOG.warning(event, feed=repr(feed_key), **fields)


def _emit_env_warnings(parsed: _ParsedEnv | None) -> None:
    """The §2.3 non-ACTIVE-key warn tiers (fired at resolve time, warn-once)."""
    if parsed is None:
        return
    active = active_feeds()
    for key, token in parsed.per_feed.items():
        if key not in DEFAULT_FEED_SCOPES:
            _warn_once(
                "feed_scope.unregistered_feed_binding",
                key,
                token=token,
                hint="inert until the feed registers — verify spelling",
            )
        if key not in active and token == SCOPE_FULL:
            _warn_once(
                "feed_scope.inactive_feed_wide_binding",
                key,
                token=token,
                hint=(
                    "wide binding on a feed with no registered adapter — excluded "
                    "from the read union until the feed registers"
                ),
            )


def _scope_for_token(feed: str, token: str) -> FeedScope:
    """Materialize the :class:`FeedScope` for one resolved *token*."""
    if token == SCOPE_FULL:
        return FeedScope(
            feed=feed, token=SCOPE_FULL, admitted_underlyings=None, fno_underlyings=None
        )
    return FeedScope(
        feed=feed,
        token=SCOPE_INDEX4,
        admitted_underlyings=INDEX4_UNDERLYINGS,
        fno_underlyings=INDEX4_FNO_UNDERLYINGS,
    )


def resolve_feed_scope(feed: str = DEFAULT_FEED) -> FeedScope:
    """Resolve *feed*'s scope from env + defaults — the ACTING-feed law (§2.4).

    Memoized per ``(feed, raw env string)``. Resolution order: an env CSV key
    for *feed* wins; the bare-token form covers scope-KNOWN feeds (the
    :data:`DEFAULT_FEED_SCOPES` keys); else the defaults map; else the
    fail-closed ``index4`` (the safe direction) — with ONE warn-once
    ``feed_scope.active_feed_fail_closed_default`` when the falling-back feed
    is ACTIVE and the env IS set (§2.3 Rev 5 — otherwise a bare ``full``
    silently leaves a just-registered scope-unknown feed narrow with zero
    signal). Junk env raises :class:`FeedScopeError` (the eager-validation
    sites make that an exit-1 boot / clean 400).
    """
    raw = os.environ.get(FEED_SCOPE_ENV)
    parsed = _parse_env(raw)
    _emit_env_warnings(parsed)
    key = (feed.strip().lower(), raw)
    with _LOCK:
        cached = _feed_memo.get(key)
    if cached is not None:
        return cached
    feed_key = key[0]
    token: str | None = None
    if parsed is not None:
        if parsed.bare_token is not None:
            if feed_key in DEFAULT_FEED_SCOPES:
                token = parsed.bare_token
        else:
            token = parsed.per_feed.get(feed_key)
    if token is None:
        token = DEFAULT_FEED_SCOPES.get(feed_key)
    if token is None:
        # Absent from BOTH env and defaults → fail-closed narrow (§2.3).
        if parsed is not None and feed_key in active_feeds():
            _warn_once(
                "feed_scope.active_feed_fail_closed_default",
                feed_key,
                hint=("resolving index4 — bind it explicitly or add it to DEFAULT_FEED_SCOPES"),
            )
        token = SCOPE_INDEX4
    scope = _scope_for_token(feed_key, token)
    with _LOCK:
        _feed_memo[key] = scope
    return scope


def resolve_read_scope() -> FeedScope:
    """The read-union scope: UNION over every ACTIVE feed's scope (§2.4).

    Memoized on the COMPOSITE ``(raw env string, active_feeds() snapshot)``
    key (§2.3 memo law) — a §2.5 registration moves the snapshot, so
    widen-by-registration reaches a primed memo with no env movement and no
    restart. Any ACTIVE feed bound ``full`` ⇒ the union is unbounded;
    non-active bindings never fold in. Since the union ⊇ every active feed's
    scope, a display can never hide what a producer admits.

    A caller that ALSO folds :func:`scope_revision` into a cache key must use
    :func:`read_scope_snapshot` instead of pairing two separate calls — the
    pair law (§2.5 coherence).
    """
    return _read_union(os.environ.get(FEED_SCOPE_ENV), active_feeds())


def read_scope_snapshot() -> tuple[FeedScope, str]:
    """One COHERENT ``(read-union scope, scope_revision token)`` pair (§2.5).

    Derives BOTH values from a SINGLE ``(env raw, active_feeds())`` snapshot,
    so a concurrent §2.5 registration landing between two SEPARATE
    :func:`resolve_read_scope` / :func:`scope_revision` reads can never pair
    a pre-widening (narrow) scope with the post-widening token — which would
    cache the narrow payload UNDER the post-widening key and keep serving it
    to post-registration requests until an unrelated generation/master bump,
    silently defeating the token fold's stated purpose. Every memo-key site
    that threads the scope AND folds the token binds this helper
    (source-pinned); a LONE token fold whose compute resolves the scope
    lazily stays on :func:`scope_revision` — that ordering is benign (a
    stale-token-keyed entry simply misses after the registration).
    """
    raw = os.environ.get(FEED_SCOPE_ENV)
    active = active_feeds()
    return _read_union(raw, active), _revision_token(raw, active)


def _read_union(raw: str | None, active: frozenset[str]) -> FeedScope:
    """Compute (memoized) the read union for one ``(raw, active)`` snapshot."""
    parsed = _parse_env(raw)
    _emit_env_warnings(parsed)
    key = (raw, active)
    with _LOCK:
        cached = _union_memo.get(key)
    if cached is not None:
        return cached
    scopes = [resolve_feed_scope(feed) for feed in sorted(active)]
    if any(not scope.bounded() for scope in scopes):
        union = FeedScope(
            feed=READ_UNION_FEED, token=SCOPE_FULL, admitted_underlyings=None, fno_underlyings=None
        )
    else:
        admitted: frozenset[tuple[Exchange, str]] = frozenset().union(
            *(scope.admitted_underlyings or frozenset() for scope in scopes)
        )
        fno: frozenset[tuple[Exchange, str]] = frozenset().union(
            *(scope.fno_underlyings or frozenset() for scope in scopes)
        )
        union = FeedScope(
            feed=READ_UNION_FEED,
            token=SCOPE_INDEX4,
            admitted_underlyings=admitted,
            fno_underlyings=fno,
        )
    with _LOCK:
        _union_memo[key] = union
    return union


def note_feed_registered(feed: str) -> None:
    """Grow the ACTIVE-feed set — called ONLY from the §2.5 registration seam.

    ``VendorProfileRegistry.register`` calls this exactly where profile state
    LANDS (after every refusal arm), so a REFUSED registration never grows the
    set. Idempotent; a blank token is a defensive no-op (profile construction
    already refuses empty vendor tokens). The union memo needs no explicit
    invalidation — it keys on the active SNAPSHOT.
    """
    feed_key = feed.strip().lower()
    if not feed_key:
        return
    with _LOCK:
        _active.add(feed_key)


def active_feeds() -> frozenset[str]:
    """The ACTIVE (adapter-registered) feeds — seeded ``{"groww"}`` (§2.5)."""
    with _LOCK:
        return frozenset(_active)


def scope_revision() -> str:
    """The read-scope identity token — derived from (env string, active set).

    Moves on a §2.5 registration (and on an env change across restarts);
    folded into the non-expiring read-cache keys (the #1650
    ``master_revision``-fold idiom) so registration widening reaches primed
    caches without a write-generation bump. A caller that ALSO threads the
    read-union scope must use :func:`read_scope_snapshot` — the pair law.
    """
    return _revision_token(os.environ.get(FEED_SCOPE_ENV), active_feeds())


def _revision_token(raw: str | None, active: frozenset[str]) -> str:
    """Derive the identity token for one ``(raw, active)`` snapshot.

    The active members are repr()-joined (NOT bare-comma-joined): vendor
    tokens are validated only as non-empty strings, so a comma-bearing token
    (``"a,b"``) bare-joined would ALIAS two different active-set snapshots
    onto one token — exactly the registration-widening staleness class this
    token exists to move on (DEC-UNIVERSE-INDEX-ONLY-001 review item 2; a
    repr of a str quotes + escapes its content, so the joined form is
    unambiguous). No in-tree token carries a comma — hardening, matching the
    already-repr'd env half.
    """
    basis = f"{raw!r}|{','.join(repr(feed) for feed in sorted(active))}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def validate_feed_scope_env() -> FeedScope:
    """Eagerly validate ``BRUTEX_FEED_SCOPE`` — the §2.3 boot/entry seam.

    Called once, pre-lock, by every scope-consuming entry point — the NINE
    CLI entrypoints (``cmd_dashboard`` / ``cmd_fetch`` / ``cmd_pull_all_spot``
    / ``cmd_nightly_run`` / ``cmd_greeks_backfill`` / ``cmd_enrich_greeks`` /
    the crossverify ``run`` CLI entry / ``cmd_doctor`` / ``cmd_doctor_live``)
    plus the dashboard greeks-backfill route — so junk fails at exit-1 boot /
    clean 400 — never mid-pull (the #1558 r5/r6 eager-validation law).
    Returns the acting default feed's scope for convenience.
    """
    return resolve_feed_scope(DEFAULT_FEED)


def _reset_for_tests() -> None:
    """Reset ALL process-global state — the house test-isolation seam.

    ACTIVE → the ``{"groww"}`` seed; every memo + the warn-once registry
    cleared (the feed_rust/bad_tick warn-once-registry idiom). Tests only.
    """
    with _LOCK:
        _active.clear()
        _active.update(_ACTIVE_SEED)
        _parse_memo.clear()
        _feed_memo.clear()
        _union_memo.clear()
        _warned.clear()


__all__ = [
    "DEFAULT_FEED",
    "DEFAULT_FEED_SCOPES",
    "ENV_FEED_SCOPE",
    "FEED_SCOPE_ENV",
    "INDEX4_FNO_UNDERLYINGS",
    "INDEX4_UNDERLYINGS",
    "READ_UNION_FEED",
    "SCOPE_FULL",
    "SCOPE_INDEX4",
    "FeedScope",
    "FeedScopeError",
    "active_feeds",
    "note_feed_registered",
    "read_scope_snapshot",
    "resolve_feed_scope",
    "resolve_read_scope",
    "scope_revision",
    "validate_feed_scope_env",
]
