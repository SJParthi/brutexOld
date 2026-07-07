"""Groww derivative-contract resolution — expiries → contracts → parsed chain.

DEC-GROWW-OPTION-INGEST-001 (K1). The pure resolution leaf of the option ingest
path: it turns the SDK's expired-contract addressing into vendor-neutral
:class:`~brutex.adapters.protocol.DerivativeContract` rows, ready to feed to
:func:`~brutex.adapters.groww.option_ingest.fetch_option_candles`.

The SDK chain (``docs/groww/api-reference/03-backtesting.md:41-134``):

1. :func:`resolve_expiries` — ``groww.get_expiries(exchange, underlying[, year[, month]])``
   → the ``YYYY-MM-DD`` expiry dates. ``year``/``month`` are OPTIONAL (omit → the
   current year / the whole year), so :func:`enumerate_contracts` issues ONE call
   per historical year to cover the full ``available from 2020`` FNO depth.
2. :func:`resolve_contracts` — ``groww.get_contracts(exchange, underlying, expiry)``
   → the listed Groww symbols (ALL listed strikes, NOT relative-to-spot), each
   parsed by :func:`parse_contract_symbol`.

Everything here takes an injected :class:`~brutex.adapters.groww.client.GrowwClient`
(the only network surface; the SDK calls ride its ``pace`` rate gate), so every
branch is unit-testable with a fake — no ``growwapi``, no network. The parser is
pure.

**Scope (CLAUDE.md §3):** the contracts these functions resolve are
**LAKE / REFERENCE_ONLY** (DEC-P0-063) — never the brute-force engine or the
52-condition vocabulary.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation

from brutex.adapters.groww.client import GrowwClientProtocol
from brutex.adapters.groww.symbol_form_preference import SymbolFormPreference
from brutex.adapters.ingest_common import DEFAULT_EMPTY_RETRY_ATTEMPTS, fetch_until_nonempty
from brutex.adapters.protocol import DerivativeContract

#: The Groww symbol-token suffix → contract type. ``FUT`` carries no strike;
#: ``CE`` / ``PE`` carry one (03-backtesting.md:16-22).
_FUTURE: str = "FUT"
_OPTION_TYPES: frozenset[str] = frozenset({"CE", "PE"})
#: Min token count of a Groww derivative symbol: ``EXCH-SYMBOL-EXPIRY-FUT`` (4)
#: for a future; an option adds a STRIKE token (``…-STRIKE-TYPE``) → 5.
_MIN_FUTURE_TOKENS: int = 4
_MIN_OPTION_TOKENS: int = 5


def parse_contract_symbol(
    symbol: str, *, underlying_symbol: str, expiry: date
) -> DerivativeContract:
    """Parse one Groww contract ``groww_symbol`` into a :class:`DerivativeContract`.

    Groww contract symbols are ``EXCH-SYMBOL-DDMmmYY[-STRIKE]-TYPE`` (e.g.
    ``NSE-NIFTY-30Sep25-24650-CE``, ``NSE-NIFTY-30Sep25-FUT``,
    ``BSE-SENSEX-25Sep25-79500-PE`` — 03-backtesting.md:33-35). Parsed
    **right-anchored** so a (hypothetical) hyphenated underlying never confuses
    the type/strike tokens: the LAST token is the type (``CE`` / ``PE`` / ``FUT``)
    and, for an option, the second-to-last is the strike.

    The ``underlying_symbol`` and ``expiry`` are taken from the **authoritative**
    ``get_contracts`` call arguments (every returned contract belongs to that one
    expiry — 03-backtesting.md:91), NOT re-parsed from the lossy ``DDMmmYY`` token,
    so there is no year-window / locale ambiguity.

    Parameters
    ----------
    symbol
        The full Groww contract symbol.
    underlying_symbol
        The underlying the contract was resolved for (e.g. ``"NIFTY"``).
    expiry
        The authoritative expiry date the contract belongs to.

    Returns
    -------
    DerivativeContract
        ``contract_type`` ``FUT`` (``strike=None``) or ``CE`` / ``PE`` (with the
        parsed ``strike`` as a ``float``). The :class:`DerivativeContract`
        constructor enforces the strike-required-for-options invariant.

    Raises
    ------
    ValueError
        On a non-derivative / malformed symbol: too few tokens, an unknown trailing
        type, or an unparseable strike (fail-loud — Golden Rule #1, never a silent
        skip of a vendor format drift).
    """
    tokens = symbol.split("-")
    if len(tokens) < _MIN_FUTURE_TOKENS:
        raise ValueError(f"not a Groww derivative symbol: {symbol!r}")
    contract_type = tokens[-1]
    if contract_type == _FUTURE:
        strike: float | None = None
    elif contract_type in _OPTION_TYPES:
        if len(tokens) < _MIN_OPTION_TOKENS:
            raise ValueError(f"option symbol missing strike token: {symbol!r}")
        strike = _parse_strike(tokens[-2], symbol=symbol)
    else:
        raise ValueError(f"unknown contract type {contract_type!r} in {symbol!r}")
    return DerivativeContract(
        underlying_symbol=underlying_symbol,
        expiry=expiry,
        symbol=symbol,
        contract_type=contract_type,
        strike=strike,
    )


def _parse_strike(token: str, *, symbol: str) -> float:
    """Parse a strike token to ``float`` via :class:`~decimal.Decimal` (exact, then float).

    Parsed through ``Decimal`` first so a non-numeric strike fails loudly (the
    :class:`DerivativeContract` field is ``float | None``, so the value is stored
    as ``float`` — the strike is metadata; the candle fetch keys on the full
    ``groww_symbol`` string, never the numeric strike).
    """
    try:
        return float(Decimal(token))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"unparseable strike {token!r} in {symbol!r}") from exc


def _expiries_array(payload: Mapping[str, object]) -> list[object]:
    """Extract the ``"expiries"`` array from a ``get_expiries`` payload (lenient empty).

    A MISSING ``"expiries"`` key is treated as "no expiries" (an underlying with no
    derivatives listed for the requested year is legitimate), but a PRESENT-yet-
    non-array value is a format drift and raises (Golden Rule #1).
    """
    raw = payload.get("expiries", [])
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"Groww 'expiries' must be an array, got {type(raw).__name__}: {raw!r}")
    return list(raw)


def underlying_symbol_forms(underlying_symbol: str) -> tuple[str, ...]:
    """The underlying-symbol forms to try, in order, for the F&O expiries/contracts fetch.

    DEC-GROWW-AMPERSAND-FALLBACK-001. An ``&`` in the NSE trading symbol (``M&M``,
    ``M&MFIN``, ``L&TFH``, ``GVT&D``, ``J&KBANK``, ``ARE&M``, ``GMRP&UI``) is the
    HTTP query-string separator; the ``growwapi`` SDK's request builder sends it
    UN-ENCODED, so ``get_expiries`` / ``get_contracts`` return a clean-but-EMPTY ``200`` and
    the WHOLE underlying's expiry/contract chain is silently missed (the operator's "M&M
    fails for entire expiries and contracts" — root-caused in MIGRATION.md #870). We try the
    PLAIN symbol FIRST — zero behaviour change for the common no-``&`` case AND for any vendor
    that already handles ``&`` correctly — then the percent-encoded ``%26`` variant as the
    fallback (the standard encoding of ``&``).

    **Honest envelope (Golden Rule #1/#10):** the EXACT correct encoding is an UNVERIFIED
    vendor/SDK behaviour (the SDK's internal URL construction needs a live Groww probe —
    CLAUDE.md §6 Phase-0 probe discipline). Try-both is the no-worse-than-before approach: a
    non-``&`` symbol yields ONE form (no extra call); an ``&`` symbol yields two and uses the
    FIRST non-empty — so if the vendor handles plain ``&`` we never regress, and if neither
    form yields data we record the same empty as today (never a fabricated chain). If the SDK
    double-encodes (``%26`` → ``%2526``) the ``%26`` form simply also returns empty and we are
    no worse off; a confirmed encoding can replace this hypothesis once the probe lands.
    """
    if "&" not in underlying_symbol:
        return (underlying_symbol,)
    return (underlying_symbol, underlying_symbol.replace("&", "%26"))


def _fetch_first_nonempty_form[T](
    forms: tuple[str, ...],
    fetch: Callable[[str], T],
    *,
    is_empty: Callable[[T], bool],
) -> T:
    """Try each underlying-symbol *form* in order; return the first non-empty *fetch* result.

    DEC-GROWW-AMPERSAND-FALLBACK-001 — the symbol-form axis of the empty-retry (distinct from
    the transient-glitch :func:`~brutex.adapters.ingest_common.fetch_until_nonempty`, which
    re-fetches the SAME args). The first form (the plain symbol) is ALWAYS called; a later
    form is tried ONLY when every prior form was empty, and the LAST form's (empty) result is
    returned when all are empty — so the existing empty-record behaviour is preserved.
    O(forms): exactly 1 call for the common single-form (no-``&``) symbol, at most 2 for an
    ``&`` symbol. (Composed with the outer ``fetch_until_nonempty`` transient-retry budget of
    ``empty_retry_attempts``, a genuinely-all-empty ``&`` symbol fires at most
    ``2 × empty_retry_attempts`` SDK calls — bounded, rate-paced, and the rare worst case.)
    """
    result = fetch(forms[0])
    for form in forms[1:]:
        if not is_empty(result):
            return result
        result = fetch(form)
    return result


def resolve_expiries(
    client: GrowwClientProtocol,
    *,
    exchange: str,
    underlying_symbol: str,
    year: int | None = None,
    month: int | None = None,
    empty_retry_attempts: int = DEFAULT_EMPTY_RETRY_ATTEMPTS,
    form_preference: SymbolFormPreference | None = None,
    preference_key: str | None = None,
) -> list[date]:
    """Resolve an underlying's FNO expiry dates for *year*/*month* (sorted, deduped).

    Calls ``client.expiries`` (``year``/``month`` omitted from the SDK call when
    ``None`` — the documented "current year / whole year" defaults) and parses the
    ``YYYY-MM-DD`` strings to :class:`datetime.date`.

    **Pull-until-successful (DEC-EMPTY-RETRY-EVERYWHERE-001):** a clean-but-EMPTY
    ``get_expiries`` is AMBIGUOUS — genuine "no expiries this query" vs a transient vendor
    glitch — and trusting an empty on the FIRST call would make a re-pull silently MISS a
    newly-listed weekly/monthly expiry. So the call is re-fetched up to *empty_retry_attempts*
    times via :func:`~brutex.adapters.ingest_common.fetch_until_nonempty` (first non-empty
    wins; bounded). A real empty (e.g. a pre-listing year) is recorded only after the budget.

    *preference_key* (DEC-FORM-WINNER-PERSIST-001, the ``candles.py`` precedent): the
    key the injected *form_preference* records/orders under — default ``None`` keys by
    *underlying_symbol* verbatim (byte-identical for every existing caller). The
    metadata pull passes the PRIMARY F&O underlying while querying with a fallback
    SPELLING form, so one durable winner row serves every phase instead of
    fragmenting per spelling.

    Raises
    ------
    ValueError
        If the ``"expiries"`` value is not an array or any entry is not a parseable
        ISO date string (fail-loud — Golden Rule #1).
    """

    def _fetch_form(symbol_form: str) -> list[date]:
        payload = client.expiries(
            exchange=exchange, underlying_symbol=symbol_form, year=year, month=month
        )
        out: set[date] = set()
        for item in _expiries_array(payload):
            if not isinstance(item, str):
                raise ValueError(
                    f"Groww expiry must be a string, got {type(item).__name__}: {item!r}"
                )
            try:
                out.add(date.fromisoformat(item))
            except ValueError as exc:
                raise ValueError(f"unparseable Groww expiry: {item!r}") from exc
        return sorted(out)

    # DEC-GROWW-AMPERSAND-FALLBACK-001: try the plain symbol, then the `%26`-encoded `&`
    # variant on an empty reply, BEFORE the transient-glitch empty-retry budget.
    # DEC-SYMBOL-FORM-PREFERENCE-001: an injected preference moves the last-winning form
    # FIRST (recording on every non-empty form result) — a pure reorder, never a drop.
    # DEC-FORM-WINNER-PERSIST-001: the memory keys on *preference_key* (default: the
    # queried underlying_symbol) so the metadata pull can normalise onto the PRIMARY key.
    canonical_forms = underlying_symbol_forms(underlying_symbol)
    pref_key = underlying_symbol if preference_key is None else preference_key

    def _fetch_form_recording(symbol_form: str) -> list[date]:
        result = _fetch_form(symbol_form)
        if result and form_preference is not None:
            form_preference.record_winner(pref_key, canonical_forms, symbol_form)
        return result

    def _fetch() -> list[date]:
        forms = (
            canonical_forms
            if form_preference is None
            else form_preference.order(pref_key, canonical_forms)
        )
        return _fetch_first_nonempty_form(forms, _fetch_form_recording, is_empty=lambda r: not r)

    return fetch_until_nonempty(_fetch, is_empty=lambda r: not r, attempts=empty_retry_attempts)


def resolve_contracts(
    client: GrowwClientProtocol,
    *,
    exchange: str,
    underlying_symbol: str,
    expiry: date,
    empty_retry_attempts: int = DEFAULT_EMPTY_RETRY_ATTEMPTS,
    form_preference: SymbolFormPreference | None = None,
    preference_key: str | None = None,
) -> list[DerivativeContract]:
    """Resolve every listed contract for an ``(underlying, expiry)`` (parsed, deduped).

    Calls ``client.contracts`` and parses each returned Groww symbol via
    :func:`parse_contract_symbol`, stamping the authoritative *expiry*. Duplicate
    symbols (first-wins) are dropped via an O(1) ``set`` membership — never a
    nested scan.

    **Pull-until-successful (DEC-EMPTY-RETRY-EVERYWHERE-001):** a clean-but-EMPTY
    ``get_contracts`` for a freshly-discovered expiry is re-fetched up to
    *empty_retry_attempts* times (:func:`~brutex.adapters.ingest_common.fetch_until_nonempty`,
    first non-empty wins, bounded) so a transient vendor glitch can't drop a newly-listed
    expiry's whole strike chain.

    *form_preference* (DEC-SYMBOL-FORM-PREFERENCE-001) optionally reorders the
    ``&``-symbol form fallback so the LAST-WINNING form is tried first (never dropping
    a form); ``None`` (the default) keeps the fixed plain-first order byte-identical.
    *preference_key* (DEC-FORM-WINNER-PERSIST-001) keys that memory — default ``None``
    keys by *underlying_symbol* verbatim (byte-identical for existing callers); the
    metadata pull passes the PRIMARY F&O underlying so the durable winner row never
    fragments across spelling forms.

    Raises
    ------
    ValueError
        If the ``"contracts"`` value is not an array, an entry is not a string, or
        a symbol fails :func:`parse_contract_symbol` (fail-loud).
    """

    def _fetch_form(symbol_form: str) -> list[DerivativeContract]:
        payload = client.contracts(
            exchange=exchange, underlying_symbol=symbol_form, expiry_date=expiry.isoformat()
        )
        raw = payload.get("contracts", [])
        if not isinstance(raw, (list, tuple)):
            raise ValueError(
                f"Groww 'contracts' must be an array, got {type(raw).__name__}: {raw!r}"
            )
        seen: set[str] = set()
        contracts: list[DerivativeContract] = []
        for item in raw:
            if not isinstance(item, str):
                raise ValueError(
                    f"Groww contract must be a string, got {type(item).__name__}: {item!r}"
                )
            if item in seen:
                continue
            seen.add(item)
            # The vendor returns the canonical symbol (real `&`), so parse against the
            # ORIGINAL underlying_symbol — only the FETCH arg uses the encoded form.
            contracts.append(
                parse_contract_symbol(item, underlying_symbol=underlying_symbol, expiry=expiry)
            )
        return contracts

    # DEC-GROWW-AMPERSAND-FALLBACK-001: try the plain symbol, then the `%26`-encoded `&`
    # variant on an empty reply, BEFORE the transient-glitch empty-retry budget.
    # DEC-SYMBOL-FORM-PREFERENCE-001: an injected preference moves the last-winning form
    # FIRST (recording on every non-empty form result) — a pure reorder, never a drop.
    # DEC-FORM-WINNER-PERSIST-001: the memory keys on *preference_key* (default: the
    # queried underlying_symbol) so the metadata pull can normalise onto the PRIMARY key.
    canonical_forms = underlying_symbol_forms(underlying_symbol)
    pref_key = underlying_symbol if preference_key is None else preference_key

    def _fetch_form_recording(symbol_form: str) -> list[DerivativeContract]:
        result = _fetch_form(symbol_form)
        if result and form_preference is not None:
            form_preference.record_winner(pref_key, canonical_forms, symbol_form)
        return result

    def _fetch() -> list[DerivativeContract]:
        forms = (
            canonical_forms
            if form_preference is None
            else form_preference.order(pref_key, canonical_forms)
        )
        return _fetch_first_nonempty_form(forms, _fetch_form_recording, is_empty=lambda r: not r)

    return fetch_until_nonempty(_fetch, is_empty=lambda r: not r, attempts=empty_retry_attempts)


def option_contracts(contracts: Iterable[DerivativeContract]) -> list[DerivativeContract]:
    """Filter a contract chain to the OPTION (``CE`` / ``PE``) legs only.

    The option ingest feeds **CE/PE** symbols to the candle fetch; the FUT legs are
    the subject of :func:`future_contracts` instead. O(n), order-preserving.
    """
    return [c for c in contracts if c.contract_type in _OPTION_TYPES]


def future_contracts(contracts: Iterable[DerivativeContract]) -> list[DerivativeContract]:
    """Filter a contract chain to the FUTURE (``FUT``) legs only.

    The mirror of :func:`option_contracts` for the futures ingest
    (DEC-GROWW-FUTURE-INGEST-001): one expiry-dated future per expiry (no strike
    fan), fed to the SAME contract-agnostic candle fetch as options. O(n),
    order-preserving.
    """
    return [c for c in contracts if c.contract_type == _FUTURE]


def enumerate_contracts(
    client: GrowwClientProtocol,
    *,
    exchange: str,
    underlying_symbol: str,
    years: Iterable[int],
    form_preference: SymbolFormPreference | None = None,
    preference_key: str | None = None,
) -> Iterator[DerivativeContract]:
    """Lazily yield EVERY derivative contract for an underlying across *years*.

    For each year, resolve its expiries (one ``get_expiries`` call) and, for each
    expiry not already seen this run, yield its parsed contracts. Expiries are
    deduped across adjacent-year queries via an O(1) ``set`` so a leg-set is never
    fetched twice. A generator (lazy) so a caller can stop early.

    *form_preference* / *preference_key* (DEC-FORM-WINNER-PERSIST-001) are passed
    through VERBATIM to every :func:`resolve_expiries` / :func:`resolve_contracts`
    call, so an enumeration shares the same learned ``&``-form winners as the rest
    of the pull; the ``None`` defaults are byte-identical to before.

    **Honest envelope (Golden Rule #10):** network-bound — one ``get_expiries`` per
    year + one ``get_contracts`` per discovered expiry — NOT O(1)-total; the
    per-call rate gate is the injected ``client.pace``.
    """
    seen_expiries: set[date] = set()
    for year in years:
        for expiry in resolve_expiries(
            client,
            exchange=exchange,
            underlying_symbol=underlying_symbol,
            year=year,
            form_preference=form_preference,
            preference_key=preference_key,
        ):
            if expiry in seen_expiries:
                continue
            seen_expiries.add(expiry)
            yield from resolve_contracts(
                client,
                exchange=exchange,
                underlying_symbol=underlying_symbol,
                expiry=expiry,
                form_preference=form_preference,
                preference_key=preference_key,
            )


__all__ = [
    "enumerate_contracts",
    "future_contracts",
    "option_contracts",
    "parse_contract_symbol",
    "resolve_contracts",
    "resolve_expiries",
    "underlying_symbol_forms",
]
