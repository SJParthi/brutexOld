"""Learned ordering of the ``&``-symbol FORM fallback (DEC-SYMBOL-FORM-PREFERENCE-001
+ the durable cross-pull map of DEC-FORM-WINNER-PERSIST-001).

DEC-GROWW-AMPERSAND-FALLBACK-001 (#898) / DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001 gave
every ``&``-containing underlying (``M&M``, ``M&MFIN``, ``L&TFH``, …) a TWO-form fetch:
the plain symbol FIRST, then the ``%26``-encoded variant on a clean-empty reply. The
form ORDER was FIXED, so a symbol whose ``%26`` form is the repeat winner still paid
the full wasted plain-form fetch (all tiled windows × the empty-retry budget) on EVERY
subsequent call. This module adds the missing memory: a small, thread-safe preference
that remembers which form ROLE last returned non-empty data per key and moves that
role FIRST on the next call — while NEVER dropping, adding, or substituting a form
(the try-both safety law of #898 is untouched; the loser is still tried on an empty
first result).

Design (COMMON · injected · honest):

* **Role-indexed, not string-keyed.** The winner is remembered as its INDEX in the
  CANONICAL forms tuple (:func:`underlying_symbol_forms`
  — plain=0, encoded=1), so one underlying's learning transfers across its contracts:
  the encoded CANDLE symbol of ``NSE-M&M-…-CE`` differs per contract, but the "encoded
  form wins" fact is shared under the ``M&M`` key.
* **Injected, never a module global** (CLAUDE.md §4 "no global mutable state"): the F&O
  pull spine constructs ONE instance per pull run and threads it down; every other
  caller defaults to ``None`` and is byte-identical to the fixed order.
* **Never narrows the search.** ``order`` only PERMUTES the canonical tuple; an unknown
  key / stale index returns the input unchanged; all forms are still tried on empties.
  "If uncertain, fetch" is preserved verbatim.
* **O(1)** per lookup/record; a ``threading.Lock`` guards the dict (the F&O fan-out
  calls from parallel pool workers).

**Honest envelope (Golden Rule #1/#10):** the ``%26`` encoding itself remains a
vendor-UNVERIFIED hypothesis (the #898 wording stands — a live Groww probe is the only
closure; the machine-readable closure seam is the :data:`FORM_PROBE_VERDICT_KEY` stamp
row, written ONLY by a decisive ``brutex doctor --live --ampersand-probe`` run and read
back via :func:`form_probe_status` — "verified" is DATA, never prose); this module only
reorders which hypothesis is tried first. The map optimises CALL ORDER only — a stale
or wrong persisted winner costs exactly one extra empty fetch, never data (which
bars/expiries are STORED can never change).

**Cross-pull persistence (DEC-FORM-WINNER-PERSIST-001):** the base
:class:`SymbolFormPreference` stays PROCESS-LOCAL and per-pull;
:class:`PersistentSymbolFormPreference` adds the durable layer — each learned winner is
persisted as ONE ``schema_meta`` row (``groww.form_winner.<key>`` → the KEY-level
winning form STRING, i.e. the winner's ROLE applied to the key's OWN canonical tuple,
so the metadata phase and every per-contract candle fetch read/write ONE shared value
space) through the injected :class:`~brutex.data.meta_kv.MetaKV` store (the
``vendor_floor`` durable-learned-value pattern), read back lazily on the first
``order`` per key per preference INSTANCE (production wires ONE instance per pull
run, so a long-lived dashboard pays at most one seed read per ``&`` key per pull —
NOT a per-process bound, and deliberately so: a fresh run picks up rows written by
another process, e.g. the doctor probe) — so a fresh pull issues the learned winner
FIRST on its very first call, never re-probing what is known. This supersedes the
"cross-pull persistence … is a NAMED follow-up" clause of
DEC-SYMBOL-FORM-PREFERENCE-001.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import structlog

from brutex.config.deadline import resolve_env_seconds, run_with_deadline
from brutex.data.meta_kv import MetaKV

_LOG = structlog.get_logger("brutex.adapters.groww.symbol_form_preference")

__all__ = [
    "FORM_PROBE_VERDICT_KEY",
    "FORM_WINNER_META_PREFIX",
    "FORM_WINNER_READ_DEADLINE_S",
    "FORM_WINNER_WRITE_DEADLINE_S",
    "FormProbeStatus",
    "PersistentSymbolFormPreference",
    "SymbolFormPreference",
    "form_probe_status",
    "form_winner_key",
    "form_winner_persist_enabled",
    "parse_form_winner",
    "underlying_symbol_forms",
]

#: ``schema_meta`` key prefix for a learned per-key ``&``-form winner. The full key is
#: ``groww.form_winner.<key>`` (key = the PRIMARY underlying for the option chain /
#: metadata paths, the groww_symbol for a self-keyed candle fetch); the value is the
#: KEY-LEVEL winning form STRING (e.g. ``"M%26M"`` under the ``M&M`` key) — the
#: winner's ROLE applied to :func:`underlying_symbol_forms` of the KEY itself, so a
#: per-contract candle win and a metadata win write the SAME value for the same role
#: (one row serves every phase, never fragmenting per call-site spelling) —
#: self-describing, immune to a canonical-tuple reordering, and healable by hand.
FORM_WINNER_META_PREFIX: Final[str] = "groww.form_winner."

#: THE machine-readable probe-verdict stamp row (DEC-FORM-WINNER-PERSIST-001).
#: Deliberately NOT under :data:`FORM_WINNER_META_PREFIX` so a bulk prefix load can
#: never confuse the stamp with a winner row. Its ONLY writer in the repo is the
#: ``brutex doctor --live --ampersand-probe`` stage on a DECISIVE probe run
#: (test-pinned); production pull code is structurally incapable of writing it, so
#: "the encoding is verified" can never be minted by ordinary fallback wins.
FORM_PROBE_VERDICT_KEY: Final[str] = "groww.form_probe.verdict"

#: Longest accepted persisted winner-form value. A real form is a short vendor symbol
#: (``M%26M`` / ``NSE-M%26M-30Sep25-2400-CE``); anything longer is corrupt data.
_MAX_FORM_WINNER_LEN: Final[int] = 64

#: ``BRUTEX_FORM_WINNER_PERSIST`` values that DISABLE the durable write-back (the
#: ``derive_stamp`` disabled-values template). Reads are unaffected — an already
#: persisted winner is still honoured; the switch only stops NEW rows being written.
_PERSIST_DISABLED_VALUES: Final[frozenset[str]] = frozenset({"0", "off", "false", "no"})

#: Wall-clock bound on the one-shot durable seed READ (``get_meta``) per ``&`` key per
#: process. A normal sidecar read is sub-millisecond; a WEDGED ``instruments.duckdb``
#: on a stalled external volume would otherwise hang the reading worker FOREVER (the
#: #999/#1522/DEC-STORE-WRITE-DEADLINE-001 hazard class — a hung call never raises, so
#: no retry ladder can help). On expiry the COMMON ``run_with_deadline`` freeze guard
#: raises and the order degrades to the canonical plain-first tuple (never a crash,
#: never a narrowed search). Env override: ``BRUTEX_FORM_WINNER_READ_DEADLINE_S``.
FORM_WINNER_READ_DEADLINE_S: Final[float] = 2.0

#: The env override for :data:`FORM_WINNER_READ_DEADLINE_S` (fail-safe parse via the
#: COMMON :func:`~brutex.config.deadline.resolve_env_seconds` — a bad value can never
#: disable the bound).
_READ_DEADLINE_ENV: Final[str] = "BRUTEX_FORM_WINNER_READ_DEADLINE_S"

#: Wall-clock bound on the write-on-change durable persist (``set_meta``) per learned
#: winner — the WRITE twin of :data:`FORM_WINNER_READ_DEADLINE_S` (review round-2
#: MAJOR: the write side shares the read side's hazard class EXACTLY — a hung DuckDB
#: write on a stalled volume never RAISES, so the WARN-degrade ``except`` alone cannot
#: help; an unbounded ``set_meta`` would hang the recording fetch worker FOREVER, the
#: pull driver awaiting it, and queue every later persist behind ``_persist_lock``).
#: On expiry the COMMON ``run_with_deadline`` freeze guard raises, the existing
#: ``form_winner.persist_failed`` WARN fires, and the pull continues on the in-memory
#: winner — one bounded wait + one abandoned daemon, the read-side contract (an
#: abandoned write that eventually lands is harmless: an idempotent same-value
#: ``INSERT OR REPLACE``). Slightly wider than the read bound because a healthy write
#: may briefly queue behind another legitimate store writer's lock. Env override:
#: ``BRUTEX_FORM_WINNER_WRITE_DEADLINE_S``.
FORM_WINNER_WRITE_DEADLINE_S: Final[float] = 5.0

#: The env override for :data:`FORM_WINNER_WRITE_DEADLINE_S` (same fail-safe parse —
#: a bad value can never disable the bound).
_WRITE_DEADLINE_ENV: Final[str] = "BRUTEX_FORM_WINNER_WRITE_DEADLINE_S"


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

    Lives HERE (moved from ``contracts.py``, which re-exports it byte-compatibly) so the
    durable winner map can role-normalise its persisted value against the KEY's own
    canonical tuple without an import cycle (``contracts`` already imports this module).

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


def form_winner_key(underlying: str) -> str:
    """The ``schema_meta`` key holding *underlying*'s learned form winner — O(1)."""
    return f"{FORM_WINNER_META_PREFIX}{underlying}"


def parse_form_winner(raw: str | None) -> str | None:
    """Parse a persisted winner-form value; malformed → ``None`` (never raises).

    Defensive (the :func:`~brutex.adapters.groww.vendor_floor.parse_vendor_floor`
    twin): a hand-edited / corrupted ``schema_meta`` value must degrade to "no
    learned winner" (the canonical plain-first order — never a crash, never a
    fabricated reorder; Golden Rule #1). ``None`` / empty / whitespace-only /
    longer than 64 chars / an embedded newline all read as absent; a value with
    incidental leading/trailing whitespace is accepted stripped.
    """
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped or len(stripped) > _MAX_FORM_WINNER_LEN:
        return None
    if "\n" in stripped or "\r" in stripped:
        return None
    return stripped


def form_winner_persist_enabled(env: Mapping[str, str] | None = None) -> bool:
    """``True`` unless the ``BRUTEX_FORM_WINNER_PERSIST`` kill switch disables writes.

    Default ON (unset / any other value): the durable map is the feature. Set
    ``BRUTEX_FORM_WINNER_PERSIST=off`` (or ``0``/``false``/``no``, case-insensitive)
    to stop NEW winner rows being written — in-memory learning + already-persisted
    reads keep working, so the switch is a write-side escape hatch, never a
    behaviour cliff. *env* is injectable for tests (defaults to ``os.environ``).
    """
    source = os.environ if env is None else env
    value = source.get("BRUTEX_FORM_WINNER_PERSIST", "")
    return value.strip().lower() not in _PERSIST_DISABLED_VALUES


class SymbolFormPreference:
    """Thread-safe per-key memory of the last WINNING symbol-form role.

    ``order`` returns the given canonical forms with the remembered winner moved
    FIRST (stable order of the rest, NEVER dropping a form); ``record_winner`` stores
    the winning form's index in the canonical tuple. Unknown keys / out-of-range
    indices are no-ops, so the preference can only ever REORDER, never narrow.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: key → the winning form's index in the CANONICAL forms tuple.
        self._winner_index: dict[str, int] = {}

    def order(self, key: str, forms: tuple[str, ...]) -> tuple[str, ...]:
        """Return *forms* with the remembered winner (if any) moved first — O(1).

        The input MUST be the CANONICAL tuple for *key*
        (:func:`underlying_symbol_forms` order) so the
        remembered index addresses the same ROLE. No memory, a canonical-first winner
        (index 0), or a stale out-of-range index returns *forms* unchanged — the
        default order is always a safe fallback, never an error.
        """
        with self._lock:
            idx = self._winner_index.get(key)
        if idx is None or idx <= 0 or idx >= len(forms):
            return forms
        return (forms[idx], *forms[:idx], *forms[idx + 1 :])

    def record_winner(self, key: str, forms: tuple[str, ...], winning_form: str) -> bool:
        """Remember that *winning_form* (∈ the CANONICAL *forms*) returned data — O(forms).

        Called ONLY on a non-empty result. A *winning_form* not present in *forms*
        (defensive — a caller bug) is ignored rather than poisoning the memory.
        ``O(forms)`` is O(2) in practice (plain + encoded). Returns ``True`` iff the
        remembered winner CHANGED (computed atomically under the map lock — the
        write-on-change seam :class:`PersistentSymbolFormPreference` persists on;
        DEC-FORM-WINNER-PERSIST-001. Every pre-existing caller ignores the return
        value, so the base behaviour is byte-identical).
        """
        try:
            idx = forms.index(winning_form)
        except ValueError:
            return False
        with self._lock:
            changed = self._winner_index.get(key) != idx
            self._winner_index[key] = idx
        return changed


class PersistentSymbolFormPreference(SymbolFormPreference):
    """The DURABLE form-winner map (DEC-FORM-WINNER-PERSIST-001).

    A drop-in :class:`SymbolFormPreference` whose learned winners survive across
    pulls AND processes as sparse ``schema_meta`` rows
    (``groww.form_winner.<key>`` → the winning form STRING) in the injected
    :class:`~brutex.data.meta_kv.MetaKV` store (production: the wired
    :class:`~brutex.data.instrument_store.InstrumentStore`; per-row
    ``INSERT OR REPLACE`` — atomic, idempotent, cross-process-safe, auto-locked).

    * ``store=None`` (dry-run / synthetic / unit-test default) is BYTE-IDENTICAL to
      the base class — nothing is read or written.
    * **O(1) hot path:** the store is consulted ONLY for a multi-form key — BOTH the
      seed read in ``order`` AND the persist in ``record_winner`` are gated
      ``len(forms) > 1``, so the ~750 plain underlyings pay ZERO store I/O on either
      path — and only ONCE per key per preference INSTANCE (a ``_loaded`` memo,
      single-flight; production constructs one instance per pull run, so a
      long-lived dashboard pays at most one seed read per ``&`` key per pull —
      deliberate, a fresh run picks up rows written by another process, e.g. the
      doctor probe); a steady-state ``order``/repeat ``record_winner`` does ZERO
      store I/O.
    * **Role-normalised value:** the persisted value is the winner's ROLE applied to
      the KEY's own :func:`underlying_symbol_forms` tuple (``"M%26M"`` under the
      ``M&M`` key — never a per-contract spelling like ``NSE-M%26M-…-CE``), so a
      candle-phase win and a metadata-phase win write the SAME value and the seed
      translates it back by role into WHATEVER canonical tuple the reader passes —
      one durable row serves every phase. (A defensive raw-string fallback covers a
      key whose own tuple cannot express the role; the seed also accepts a legacy
      raw form via direct membership.)
    * **Write-on-change:** a winner is persisted only when it CHANGED (including a
      change BACK to the plain form — self-healing if the vendor starts serving
      plain ``&``), so the parallel fan-out can never write-storm the store
      (≤ a handful of writes per lifetime for the whole ``&`` family).
    * **Lock discipline:** the map lock is NEVER held across store I/O — the seed
      READ runs outside it (the key is claimed in ``_loaded`` under the lock, the
      ``get_meta`` executes lock-free, the adopted index re-acquires for a
      ``setdefault``; a same-key racer during the in-flight read serves the
      canonical order for that one call, exactly like a load failure) and is
      wall-clock-BOUNDED by the COMMON ``run_with_deadline`` freeze guard
      (:data:`FORM_WINNER_READ_DEADLINE_S`), and the persist runs OUTSIDE the map
      lock under a dedicated ``_persist_lock``, its ``set_meta`` ALSO
      deadline-bounded (:data:`FORM_WINNER_WRITE_DEADLINE_S` — review round-2
      MAJOR: a non-raising wedged write would otherwise hang the recording fetch
      worker forever and queue every later persist behind ``_persist_lock``) — so
      a slow or WEDGED disk can never stall the fan-out's ``order`` reads NOR the
      recording workers (each wedged read/write costs its ONE bounded deadline on
      that worker plus one abandoned daemon, never a stalled fan-out or a hung
      pull).
    * **Never crashes a pull:** every store read/write is best-effort — an exception
      (a deadline expiry included) logs ONE ``form_winner.load_failed`` /
      ``form_winner.persist_failed`` WARNING and degrades to the in-memory
      behaviour (the ``vendor_floor.VendorFloorLearner._persist`` contract).
    * **Reorder-never-narrow:** unchanged from the base — an unknown / stale
      persisted form is IGNORED (canonical order), and the loser form is STILL
      tried on an empty first result (the #898 try-both law).
    """

    def __init__(self, store: MetaKV | None) -> None:
        super().__init__()
        self._store = store
        self._persist_lock = threading.Lock()
        #: Keys whose durable row was already consulted by THIS instance (hit or
        #: miss or error) — the single-flight memo that bounds store reads to one
        #: per key per instance (production wires one instance per pull run; a
        #: fresh run deliberately re-reads so another process's writes are seen).
        self._loaded: set[str] = set()

    def order(self, key: str, forms: tuple[str, ...]) -> tuple[str, ...]:
        """Base :meth:`SymbolFormPreference.order` + a lazy one-shot durable seed.

        The store is consulted ONLY when *forms* has a fallback to reorder
        (``len(forms) > 1``) AND *key* has not been consulted yet by THIS
        instance (one instance per pull run in production) — the ``_loaded``
        claim is taken under the map lock but the ``get_meta`` itself runs
        OUTSIDE it (deadline-bounded), so the parallel fan-out issues exactly
        ONE ``get_meta`` per key per instance and is NEVER serialised behind a
        slow read (a same-key caller racing the in-flight read serves the
        canonical order for that one call — the same tolerance as a load
        failure). An unknown / stale persisted form is ignored; a store error or deadline
        expiry WARNs once and the key stays claimed (no re-read storm). An
        in-process winner recorded before the first ``order`` is never clobbered
        by an older persisted row (``setdefault``).
        """
        if len(forms) > 1 and self._store is not None:
            claimed = False
            with self._lock:
                if key not in self._loaded:
                    self._loaded.add(key)
                    claimed = True
            if claimed:
                self._seed(self._store, key, forms)
        return super().order(key, forms)

    def _seed(self, store: MetaKV, key: str, forms: tuple[str, ...]) -> None:
        """Read + adopt *key*'s persisted winner (runs OUTSIDE the map lock).

        The read is bounded by the COMMON ``run_with_deadline`` freeze guard
        (:data:`FORM_WINNER_READ_DEADLINE_S`) so a WEDGED sidecar volume costs the
        ONE reading worker a bounded wait (plus one abandoned daemon), never an
        unbounded hang — and, because the map lock is not held here, never a
        stalled fan-out. The persisted value is role-TRANSLATED through the KEY's
        own canonical tuple, so an underlying-level row seeds a per-contract
        tuple (and vice versa); a legacy raw form still seeds via direct
        membership. Only the final ``setdefault`` re-acquires the map lock.
        """
        try:
            raw = run_with_deadline(
                lambda: store.get_meta(form_winner_key(key)),
                resolve_env_seconds(_READ_DEADLINE_ENV, FORM_WINNER_READ_DEADLINE_S),
                what=f"form-winner read ({key})",
                thread_name="groww-form-winner-read",
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught — a sidecar read can never break a pull
            _LOG.warning(
                "form_winner.load_failed",
                key=key,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return
        seed_form = parse_form_winner(raw)
        if seed_form is None:
            # Absent / corrupt → canonical order, never a crash (Golden Rule #1).
            return
        key_forms = underlying_symbol_forms(key)
        if seed_form in key_forms:
            # The role-normalised value space: translate the KEY-level form's ROLE
            # into THIS canonical tuple (a metadata-written "M%26M" row seeds a
            # per-contract candle tuple at the same index, and vice versa).
            idx = key_forms.index(seed_form)
        elif seed_form in forms:
            # Legacy / defensive: a raw form persisted verbatim (pre-normalisation
            # rows, or the raw fallback for a key whose own tuple cannot express
            # the role) still seeds when it is a member of THIS tuple.
            idx = forms.index(seed_form)
        else:
            # Stale (matches neither value space) → canonical order, never a
            # fabricated reorder (Golden Rule #1).
            return
        with self._lock:
            self._winner_index.setdefault(key, idx)

    def record_winner(self, key: str, forms: tuple[str, ...], winning_form: str) -> bool:
        """Base in-memory record FIRST, then a write-on-change durable persist.

        The in-memory map (under the inherited lock) is the process truth; the
        durable row is written only when the winner actually CHANGED, *forms*
        carries a real fallback (``len(forms) > 1`` — a single-form plain key is
        NEVER persisted: there is no role to remember, so the ~750 plain
        underlyings pay ZERO store I/O here too), the
        :func:`form_winner_persist_enabled` kill switch is on, and a store is
        wired — OUTSIDE the map lock, under ``_persist_lock``, with the
        ``set_meta`` itself wall-clock-BOUNDED by the COMMON
        ``run_with_deadline`` freeze guard (:data:`FORM_WINNER_WRITE_DEADLINE_S`
        — a non-raising WEDGED write degrades exactly like a raising one instead
        of hanging the recording worker forever). The persisted value is
        ROLE-NORMALISED to the KEY's own canonical tuple (see
        :data:`FORM_WINNER_META_PREFIX`) so one row serves every phase; a
        ``set_meta`` failure — a deadline expiry included — WARNs and never
        raises (a pull is NEVER crashed by the sidecar).
        """
        changed = super().record_winner(key, forms, winning_form)
        store = self._store
        if not changed or len(forms) <= 1 or store is None or not form_winner_persist_enabled():
            return changed
        # `changed` is True, so the base resolved `winning_form ∈ forms` — index()
        # cannot raise here. Persist the KEY-level form at the winner's ROLE; the
        # raw string is the defensive fallback for a key whose own canonical tuple
        # cannot express that role (e.g. a plain key recorded against a multi-form
        # tuple — the seed's membership arm reads it back).
        idx = forms.index(winning_form)
        key_forms = underlying_symbol_forms(key)
        value = key_forms[idx] if idx < len(key_forms) else winning_form
        with self._persist_lock:
            try:
                # Deadline-bounded like the seed READ (review round-2 MAJOR): a
                # WEDGED set_meta never raises, so without the freeze guard it
                # would hang this fetch worker forever AND serialise every later
                # persist behind ``_persist_lock``; on expiry the WARN below fires
                # and the pull continues on the in-memory winner.
                run_with_deadline(
                    lambda: store.set_meta(form_winner_key(key), value),
                    resolve_env_seconds(_WRITE_DEADLINE_ENV, FORM_WINNER_WRITE_DEADLINE_S),
                    what=f"form-winner persist ({key})",
                    thread_name="groww-form-winner-persist",
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught — a sidecar write can never break a pull
                _LOG.warning(
                    "form_winner.persist_failed",
                    key=key,
                    winning_form=value,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        return changed


@dataclass(frozen=True, slots=True)
class FormProbeStatus:
    """The machine-readable ``&``-encoding probe verdict (DEC-FORM-WINNER-PERSIST-001).

    Attributes
    ----------
    verified
        ``True`` ONLY when a decisive probe stamp row exists and parses — the
        DATA form of "the encoding hypothesis has live-probe evidence". Missing /
        corrupt / unreadable stamp → ``False`` (honestly UNVERIFIED, never
        fabricated).
    verdict
        The probe's overall verdict string (e.g. ``ENCODED_ONLY``), else ``None``.
    winners
        The per-underlying winning-form map recorded by the probe, else ``None``.
    transcript_path
        The probe run's transcript JSONL path (the evidence), else ``None``.
    run_id
        The probe run id, else ``None``.
    probed_at
        The probe's UTC ISO timestamp, else ``None``.
    """

    verified: bool
    verdict: str | None = None
    winners: Mapping[str, str] | None = None
    transcript_path: str | None = None
    run_id: str | None = None
    probed_at: str | None = None


def form_probe_status(store: MetaKV | None) -> FormProbeStatus:
    """Read the :data:`FORM_PROBE_VERDICT_KEY` stamp — never raises.

    ``None`` store, an absent row, corrupt JSON, a non-mapping payload, or a store
    read error all return ``FormProbeStatus(verified=False)`` — an unreadable stamp
    is honestly UNVERIFIED, never a fabricated "verified" (Golden Rule #1/#10).
    """
    if store is None:
        return FormProbeStatus(verified=False)
    try:
        raw = store.get_meta(FORM_PROBE_VERDICT_KEY)
    except Exception as exc:  # pylint: disable=broad-exception-caught — an unreadable stamp is UNVERIFIED, never a crash
        _LOG.warning(
            "form_probe.status_read_failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return FormProbeStatus(verified=False)
    if raw is None:
        return FormProbeStatus(verified=False)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        _LOG.warning("form_probe.status_corrupt", raw_prefix=raw[:80])
        return FormProbeStatus(verified=False)
    if not isinstance(payload, dict):
        _LOG.warning("form_probe.status_corrupt", raw_prefix=raw[:80])
        return FormProbeStatus(verified=False)
    winners_raw = payload.get("winners")
    winners: dict[str, str] | None = None
    if isinstance(winners_raw, dict):
        winners = {str(k): str(v) for k, v in winners_raw.items()}
    verdict = payload.get("verdict")
    return FormProbeStatus(
        verified=True,
        verdict=str(verdict) if verdict is not None else None,
        winners=winners,
        transcript_path=(
            str(payload["transcript"]) if payload.get("transcript") is not None else None
        ),
        run_id=str(payload["run_id"]) if payload.get("run_id") is not None else None,
        probed_at=str(payload["probed_at"]) if payload.get("probed_at") is not None else None,
    )
