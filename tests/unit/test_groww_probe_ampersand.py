"""The ``&``-family (%26) symbol-form probe (DEC-FORM-WINNER-PERSIST-001).

Fakes only — no network. Pins the probe's evidence discipline (BOTH forms
ALWAYS called; one canonical transcript event per physical call BEFORE any
verdict interpretation; budget spent BEFORE each call), every per-underlying
verdict branch (PLAIN_OK / ENCODED_ONLY / BOTH_OK / the error-contaminated
*_UNCONFIRMED pair — data beside a RAISED sibling seeds a winner but can never
be decisive, and a format-drift payload (a present-but-non-array value, round-3
review fix, or a non-Mapping payload, round-4) contaminates the
SAME way / NEITHER / ERROR), the
aggregate/decisive fold (incl. the MIXED family), the DYNAMIC family
derivation from the instrument master (a novel ``X&Y`` listing auto-probes),
and the bounded budget abort. Together with the stage-14 suite in
``test_doctor_live.py`` this keeps ``brutex/adapters/groww/probe.py`` at 100%
line+branch.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from decimal import Decimal

import pytest
from brutex.adapters.groww.errors import GrowwServerError
from brutex.adapters.groww.probe import (
    AMP_VERDICT_BOTH_OK,
    AMP_VERDICT_ENCODED_ONLY,
    AMP_VERDICT_ENCODED_ONLY_UNCONFIRMED,
    AMP_VERDICT_ERROR,
    AMP_VERDICT_MIXED,
    AMP_VERDICT_NEITHER,
    AMP_VERDICT_PLAIN_OK,
    AMP_VERDICT_PLAIN_OK_UNCONFIRMED,
    AmpersandProbeReport,
    ampersand_family,
    probe_ampersand_forms,
)
from brutex.models import Exchange, Instrument, InstrumentType, Segment

# ── fakes ────────────────────────────────────────────────────────────────


class _Transcript:
    """A ProbeTranscript collector mirroring the kit's canonical-JSON shape."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def event(self, name: str, /, **fields: object) -> None:
        # Prove canonical serialisability at record time (the kit sink does this).
        json.dumps({"event": name, **fields}, sort_keys=True, default=str)
        self.events.append((name, dict(fields)))


class _Budget:
    """A ProbeCallBudget spy — optionally raising at a hard cap."""

    def __init__(self, limit: int | None = None) -> None:
        self.limit = limit
        self.spent: list[str] = []

    def spend(self, count: int = 1, *, label: str) -> None:
        if self.limit is not None and len(self.spent) + count > self.limit:
            raise RuntimeError(f"budget exhausted at {label}")
        self.spent.extend([label] * count)


class _AmpSDK:
    """Scripted per-(api, form) behaviour: ``data`` / ``empty`` / ``raise``."""

    def __init__(
        self,
        *,
        expiries: Mapping[str, object] | None = None,
        contracts: Mapping[str, object] | None = None,
    ) -> None:
        #: form → "data" | "empty" | "raise" (missing → "empty").
        self._expiries = dict(expiries or {})
        self._contracts = dict(contracts or {})
        self.expiry_forms: list[str] = []
        self.contract_forms: list[str] = []

    def get_expiries(self, **kw: object) -> Mapping[str, object]:
        form = str(kw["underlying_symbol"])
        self.expiry_forms.append(form)
        mode = self._expiries.get(form, "empty")
        if mode == "raise":
            raise GrowwServerError("503 upstream")
        if mode == "data":
            return {"expiries": ["2026-07-30", "2026-08-27"]}
        return {"expiries": []}

    def get_contracts(self, **kw: object) -> Mapping[str, object]:
        form = str(kw["underlying_symbol"])
        self.contract_forms.append(form)
        mode = self._contracts.get(form, "empty")
        if mode == "raise":
            raise GrowwServerError("503 upstream")
        if mode == "data":
            return {"contracts": [f"NSE-{form}-30Jul26-2400-CE"]}
        return {"contracts": []}

    def get_historical_candles(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("the ampersand probe never fetches candles")


class _RawClient:
    """A minimal GrowwClientProtocol-shaped wrapper over :class:`_AmpSDK`.

    Deliberately NOT the real ``GrowwClient``: the probe must record the RAW
    per-call outcome, so the fake forwards verbatim (no retry ladder) — and a
    raised :class:`GrowwError` reaches the probe as-is.
    """

    def __init__(self, sdk: _AmpSDK) -> None:
        self._sdk = sdk

    def expiries(
        self,
        *,
        exchange: str,
        underlying_symbol: str,
        year: int | None = None,
        month: int | None = None,
    ) -> Mapping[str, object]:
        del exchange, year, month
        return self._sdk.get_expiries(underlying_symbol=underlying_symbol)

    def contracts(
        self, *, exchange: str, underlying_symbol: str, expiry_date: str
    ) -> Mapping[str, object]:
        del exchange, expiry_date
        return self._sdk.get_contracts(underlying_symbol=underlying_symbol)


def _probe(
    sdk: _AmpSDK, underlyings: list[str], *, budget: _Budget | None = None
) -> tuple[AmpersandProbeReport, _Transcript]:
    transcript = _Transcript()
    report = probe_ampersand_forms(
        _RawClient(sdk),  # type: ignore[arg-type]  # structural subset is deliberate
        underlyings,
        budget=budget if budget is not None else _Budget(),
        transcript=transcript,
    )
    return report, transcript


# ── per-underlying verdicts (every branch) ──────────────────────────────


class TestAmpersandVerdicts:
    def test_encoded_only(self) -> None:
        sdk = _AmpSDK(expiries={"M%26M": "data"}, contracts={"M%26M": "data"})
        report, _t = _probe(sdk, ["M&M"])
        assert report.per_underlying == {"M&M": AMP_VERDICT_ENCODED_ONLY}
        assert report.winners == {"M&M": "M%26M"}
        assert report.overall == AMP_VERDICT_ENCODED_ONLY
        assert report.decisive is True

    def test_plain_ok(self) -> None:
        sdk = _AmpSDK(expiries={"M&M": "data"}, contracts={"M&M": "data"})
        report, _t = _probe(sdk, ["M&M"])
        assert report.per_underlying == {"M&M": AMP_VERDICT_PLAIN_OK}
        assert report.winners == {"M&M": "M&M"}
        assert report.decisive is True

    def test_both_ok(self) -> None:
        sdk = _AmpSDK(
            expiries={"M&M": "data", "M%26M": "data"},
            contracts={"M&M": "data", "M%26M": "data"},
        )
        report, _t = _probe(sdk, ["M&M"])
        assert report.per_underlying == {"M&M": AMP_VERDICT_BOTH_OK}
        assert report.winners == {"M&M": "M&M"}  # BOTH_OK is decisive-for-plain
        assert report.decisive is True

    def test_neither(self) -> None:
        report, _t = _probe(_AmpSDK(), ["M&M"])
        assert report.per_underlying == {"M&M": AMP_VERDICT_NEITHER}
        assert report.winners == {}  # nothing fabricated
        assert report.decisive is False

    def test_error(self) -> None:
        sdk = _AmpSDK(expiries={"M&M": "raise", "M%26M": "raise"})
        report, _t = _probe(sdk, ["M&M"])
        assert report.per_underlying == {"M&M": AMP_VERDICT_ERROR}
        assert report.winners == {}
        assert report.decisive is False
        errors = [c.error for c in report.results]
        assert all(e is not None and e.startswith("GrowwServerError") for e in errors)

    def test_plain_data_with_encoded_error_is_unconfirmed_never_decisive(self) -> None:
        """Review-fix regression (MAJOR): a throttled side is NOT a refuted side.

        Plain data + encoded RAISED (429/5xx) previously read PLAIN_OK — a CLEAR,
        decisive-capable verdict minting a stamp whose docstring claims the other
        form "cannot" return data, though that side was never cleanly observed.
        Now: the winner still seeds (data is real evidence for the ORDER), but
        the verdict is *_UNCONFIRMED — non-clear, never decisive, never stamped.
        """
        sdk = _AmpSDK(
            expiries={"M&M": "data", "M%26M": "raise"},
            contracts={"M&M": "data", "M%26M": "raise"},
        )
        report, _t = _probe(sdk, ["M&M"])
        assert report.per_underlying == {"M&M": AMP_VERDICT_PLAIN_OK_UNCONFIRMED}
        assert report.decisive is False
        assert report.winners == {"M&M": "M&M"}  # the winner STILL seeds

    def test_encoded_data_with_plain_error_is_unconfirmed_never_decisive(self) -> None:
        """The symmetric arm: the REFUTED-direction claim needs a clean plain side."""
        sdk = _AmpSDK(
            expiries={"M%26M": "data", "M&M": "raise"},
            contracts={"M%26M": "data", "M&M": "raise"},
        )
        report, _t = _probe(sdk, ["M&M"])
        assert report.per_underlying == {"M&M": AMP_VERDICT_ENCODED_ONLY_UNCONFIRMED}
        assert report.decisive is False
        assert report.winners == {"M&M": "M%26M"}  # the winner STILL seeds

    def test_no_data_side_error_on_the_contracts_leg_alone_contaminates(self) -> None:
        """A clean-empty expiries call does NOT launder a raised contracts call."""
        sdk = _AmpSDK(
            expiries={"M%26M": "data"},  # plain expiries: clean empty
            contracts={"M%26M": "data", "M&M": "raise"},  # plain contracts: RAISED
        )
        report, _t = _probe(sdk, ["M&M"])
        assert report.per_underlying == {"M&M": AMP_VERDICT_ENCODED_ONLY_UNCONFIRMED}
        assert report.decisive is False

    def test_both_ok_is_undisturbed_by_an_incidental_error(self) -> None:
        """BOTH_OK makes two POSITIVE claims — each proven by its own data."""
        sdk = _AmpSDK(
            expiries={"M&M": "data", "M%26M": "data"},
            contracts={"M&M": "raise", "M%26M": "raise"},  # incidental errors
        )
        report, _t = _probe(sdk, ["M&M"])
        assert report.per_underlying == {"M&M": AMP_VERDICT_BOTH_OK}
        assert report.decisive is True

    def test_non_groww_exception_propagates(self) -> None:
        """A code bug is NEVER folded into a verdict — it stays loud."""

        class _BuggySDK(_AmpSDK):
            def get_expiries(self, **kw: object) -> Mapping[str, object]:
                raise ValueError("a genuine code bug")

        with pytest.raises(ValueError, match="a genuine code bug"):
            _probe(_BuggySDK(), ["M&M"])


# ── evidence discipline ──────────────────────────────────────────────────


class TestAmpersandEvidence:
    def test_both_forms_always_called_on_both_apis(self) -> None:
        """Unlike production, the probe NEVER short-circuits on a non-empty plain."""
        sdk = _AmpSDK(
            expiries={"M&M": "data", "M%26M": "data"},
            contracts={"M&M": "data", "M%26M": "data"},
        )
        report, _t = _probe(sdk, ["M&M"])
        assert sdk.expiry_forms == ["M&M", "M%26M"]
        assert sdk.contract_forms == ["M&M", "M%26M"]
        assert len(report.results) == 4  # one recorded result per physical call

    def test_one_transcript_event_per_call_before_the_verdict(self) -> None:
        """Ordering pin: every amp.call precedes the underlying's amp.verdict."""
        sdk = _AmpSDK(expiries={"M%26M": "data"}, contracts={"M%26M": "data"})
        report, transcript = _probe(sdk, ["M&M"])
        names = [name for name, _f in transcript.events]
        assert names == ["amp.call"] * 4 + ["amp.verdict"]
        first = transcript.events[0][1]
        assert first == {
            "underlying": "M&M",
            "form": "M&M",
            "role": "plain",
            "api": "expiries",
            "count": 0,
            "error": None,
        }
        assert len(report.results) == 4

    def test_budget_spent_before_every_call(self) -> None:
        budget = _Budget()
        sdk = _AmpSDK(expiries={"M%26M": "data"})
        report, _t = _probe(sdk, ["M&M"], budget=budget)
        assert budget.spent == [
            "amp.expiries.M&M",
            "amp.expiries.M&M",
            "amp.contracts.M&M",
            "amp.contracts.M&M",
        ]
        assert len(report.results) == len(budget.spent)  # one spend per recorded call

    def test_budget_exhaustion_aborts_bounded(self) -> None:
        """A raising budget stops the probe MID-family — never the account."""
        budget = _Budget(limit=3)
        sdk = _AmpSDK(expiries={"M%26M": "data"})
        with pytest.raises(RuntimeError, match="budget exhausted"):
            _probe(sdk, ["M&M"], budget=budget)
        assert len(sdk.expiry_forms) == 2  # both expiries calls issued…
        assert len(sdk.contract_forms) == 1  # …but the 4th call never fired

    def test_no_expiry_from_either_form_skips_the_contracts_leg(self) -> None:
        sdk = _AmpSDK()
        _report, transcript = _probe(sdk, ["M&M"])
        assert sdk.contract_forms == []  # never a fabricated expiry
        assert [n for n, _f in transcript.events].count("amp.call") == 2

    def test_malformed_expiry_strings_are_skipped_defensively(self) -> None:
        class _JunkExpiriesSDK(_AmpSDK):
            def get_expiries(self, **kw: object) -> Mapping[str, object]:
                self.expiry_forms.append(str(kw["underlying_symbol"]))
                return {"expiries": ["not-a-date", 20260730, None]}

        sdk = _JunkExpiriesSDK(contracts={"M%26M": "data"})
        report, _t = _probe(sdk, ["M&M"])
        # Junk parses to NO nearest expiry → the contracts leg is skipped; the
        # expiries themselves still COUNT as items (BOTH forms returned 3), so
        # the verdict folds from the expiries evidence alone.
        assert sdk.contract_forms == []
        assert report.per_underlying == {"M&M": AMP_VERDICT_BOTH_OK}

    def test_non_mapping_payload_is_a_format_drift_anomaly(self) -> None:
        """Round-4 review fix: a NON-MAPPING payload is drift, never a clean 0.

        Pre-fix a non-Mapping top-level payload (the SDK returning a parsed
        list/None) counted as a CLEAN empty (``count=0``, ``error=None``) — the
        same contamination class as the round-3 non-array value, one level up
        (production's ``contracts._expiries_array`` REQUIRES a Mapping and
        crashes loudly otherwise — a fail-loud shape, not a lenient empty).
        Both sides drifting now reads ERROR, seeds nothing.
        """

        class _WeirdSDK(_AmpSDK):
            def get_expiries(self, **kw: object) -> Mapping[str, object]:
                self.expiry_forms.append(str(kw["underlying_symbol"]))
                return ["not", "a", "mapping"]  # type: ignore[return-value]

        report, _t = _probe(_WeirdSDK(), ["M&M"])
        assert report.per_underlying == {"M&M": AMP_VERDICT_ERROR}
        assert report.winners == {}  # nothing seeded from drift
        assert report.decisive is False
        assert [c.count for c in report.results] == [None, None]
        errors = [c.error for c in report.results]
        assert errors == ["format_drift: payload is list, not a mapping"] * 2

    def test_one_sided_non_mapping_payload_demotes_to_unconfirmed_never_stamps(self) -> None:
        """Round-4 review fix — the stamp-minting permutation, one level up.

        The encoded form returns real data while the plain form's calls return
        a parsed non-Mapping (``None``). Pre-fix that drift counted as an
        UNCONTAMINATED clean empty of the no-data side, so the underlying read
        ENCODED_ONLY — a CLEAR verdict that could make the family DECISIVE and
        mint the ``groww.form_probe.verdict`` stamp from an uninterpretable
        observation, with a transcript (``count=0``/``error=None``)
        indistinguishable from a genuine vendor empty. Now it demotes to
        *_UNCONFIRMED exactly like a raised GrowwError: the winner still seeds
        (data is real evidence for the ORDER), but the verdict is non-clear →
        never decisive → never the stamp.
        """

        class _PlainNoneSDK(_AmpSDK):
            def get_expiries(self, **kw: object) -> Mapping[str, object]:
                form = str(kw["underlying_symbol"])
                self.expiry_forms.append(form)
                if form == "M%26M":
                    return {"expiries": ["2026-07-30"]}
                return None  # type: ignore[return-value]  # parsed non-dict: DRIFT

        sdk = _PlainNoneSDK(contracts={"M%26M": "data"})
        report, _t = _probe(sdk, ["M&M"])
        assert report.per_underlying == {"M&M": AMP_VERDICT_ENCODED_ONLY_UNCONFIRMED}
        assert report.decisive is False  # never a DECISIVE report → never the stamp
        assert report.winners == {"M&M": "M%26M"}  # the winner STILL seeds
        drift_calls = [c for c in report.results if c.error is not None]
        assert [(c.role, c.api, c.count) for c in drift_calls] == [("plain", "expiries", None)]
        assert drift_calls[0].error == "format_drift: payload is NoneType, not a mapping"

    def test_non_array_expiries_value_is_a_format_drift_anomaly(self) -> None:
        """A PRESENT-but-non-array ``expiries`` value is a call-level anomaly.

        Round-3 review fix: pre-fix the drift counted as a CLEAN 0 (this
        two-sided variant read NEITHER), though production fail-louds on the
        exact same shape (``contracts._expiries_array``). The drift now records
        ``count=None`` + a ``format_drift`` error, contaminating the fold
        exactly like a raise — BOTH sides drifting reads ERROR, seeds nothing.
        """

        class _NonArraySDK(_AmpSDK):
            def get_expiries(self, **kw: object) -> Mapping[str, object]:
                self.expiry_forms.append(str(kw["underlying_symbol"]))
                return {"expiries": "oops-not-an-array"}

        sdk = _NonArraySDK(contracts={"M%26M": "data"})
        report, _t = _probe(sdk, ["M&M"])
        assert sdk.contract_forms == []  # no parseable expiry → contracts leg skipped
        assert report.per_underlying == {"M&M": AMP_VERDICT_ERROR}
        assert report.winners == {}  # nothing seeded from drift
        assert report.decisive is False
        assert [c.count for c in report.results] == [None, None]
        errors = [c.error for c in report.results]
        assert all(e is not None and e.startswith("format_drift:") for e in errors)

    def test_one_sided_format_drift_demotes_to_unconfirmed_never_stamps(self) -> None:
        """Round-3 review fix — the stamp-minting permutation: drift ≠ clean empty.

        The encoded form returns real data while the plain form returns a 200
        mapping whose ``expiries`` value is a non-array (``{"expiries":
        "throttled"}``). Pre-fix that drift counted as an UNCONTAMINATED clean
        empty of the no-data side, so the underlying read ENCODED_ONLY — a
        CLEAR verdict that could make the family DECISIVE and mint the
        ``groww.form_probe.verdict`` stamp from drift-contaminated negative
        evidence. Now the drift demotes to *_UNCONFIRMED exactly like a raised
        GrowwError: the winner still seeds (data is real evidence for the
        ORDER), but the verdict is non-clear → never decisive → never the stamp.
        """

        class _PlainDriftSDK(_AmpSDK):
            def get_expiries(self, **kw: object) -> Mapping[str, object]:
                form = str(kw["underlying_symbol"])
                self.expiry_forms.append(form)
                if form == "M%26M":
                    return {"expiries": ["2026-07-30"]}
                return {"expiries": "throttled"}  # present-but-non-array: DRIFT

        sdk = _PlainDriftSDK(contracts={"M%26M": "data"})
        report, _t = _probe(sdk, ["M&M"])
        assert report.per_underlying == {"M&M": AMP_VERDICT_ENCODED_ONLY_UNCONFIRMED}
        assert report.decisive is False  # never a DECISIVE report → never the stamp
        assert report.winners == {"M&M": "M%26M"}  # the winner STILL seeds
        drift_calls = [c for c in report.results if c.error is not None]
        assert [(c.role, c.api, c.count) for c in drift_calls] == [("plain", "expiries", None)]
        error = drift_calls[0].error
        assert error is not None and error.startswith("format_drift:")

    def test_contracts_leg_format_drift_also_contaminates(self) -> None:
        """The ``contracts`` key rides the SAME classification (one wrapper)."""

        class _ContractsDriftSDK(_AmpSDK):
            def get_contracts(self, **kw: object) -> Mapping[str, object]:
                form = str(kw["underlying_symbol"])
                self.contract_forms.append(form)
                if form == "M%26M":
                    return {"contracts": [f"NSE-{form}-30Jul26-2400-CE"]}
                return {"contracts": {"nested": "wrong"}}  # DRIFT on the plain side

        sdk = _ContractsDriftSDK(expiries={"M%26M": "data"})
        report, _t = _probe(sdk, ["M&M"])
        assert report.per_underlying == {"M&M": AMP_VERDICT_ENCODED_ONLY_UNCONFIRMED}
        assert report.decisive is False

    def test_missing_expiries_key_stays_a_lenient_clean_empty(self) -> None:
        """A MISSING key is production's lenient-empty arm — NEVER a drift error."""

        class _NoKeySDK(_AmpSDK):
            def get_expiries(self, **kw: object) -> Mapping[str, object]:
                self.expiry_forms.append(str(kw["underlying_symbol"]))
                return {}

        report, _t = _probe(_NoKeySDK(), ["M&M"])
        assert report.per_underlying == {"M&M": AMP_VERDICT_NEITHER}
        assert all(c.count == 0 and c.error is None for c in report.results)

    def test_single_form_name_is_skipped_never_invented(self) -> None:
        sdk = _AmpSDK(expiries={"NIFTY": "data"})
        report, transcript = _probe(sdk, ["NIFTY"])
        assert report.per_underlying == {}
        assert sdk.expiry_forms == []  # zero vendor calls for a non-& name
        assert transcript.events == [("amp.skipped_single_form", {"underlying": "NIFTY"})]


# ── aggregation ──────────────────────────────────────────────────────────


class TestAmpersandAggregation:
    def test_empty_family_is_neither_and_non_decisive(self) -> None:
        report, _t = _probe(_AmpSDK(), [])
        assert report.overall == AMP_VERDICT_NEITHER
        assert report.decisive is False
        assert report.per_underlying == {}

    def test_mixed_clear_family_is_mixed_and_decisive(self) -> None:
        """Per-underlying clear verdicts that DISAGREE: MIXED overall, decisive."""
        sdk = _AmpSDK(expiries={"M%26M": "data", "L&TFH": "data"})
        report, _t = _probe(sdk, ["M&M", "L&TFH"])
        assert report.per_underlying == {
            "M&M": AMP_VERDICT_ENCODED_ONLY,
            "L&TFH": AMP_VERDICT_PLAIN_OK,
        }
        assert report.overall == AMP_VERDICT_MIXED
        assert report.decisive is True  # every name has a CLEAR verdict
        assert report.winners == {"M&M": "M%26M", "L&TFH": "L&TFH"}

    def test_any_unclear_name_blocks_decisive(self) -> None:
        sdk = _AmpSDK(expiries={"M%26M": "data"})
        report, _t = _probe(sdk, ["M&M", "L&TFH"])  # L&TFH is all-empty → NEITHER
        assert report.per_underlying["L&TFH"] == AMP_VERDICT_NEITHER
        assert report.overall == AMP_VERDICT_MIXED
        assert report.decisive is False
        assert report.winners == {"M&M": "M%26M"}  # clear names still seed


# ── the DYNAMIC family derivation ────────────────────────────────────────


def _instrument(
    symbol: str,
    *,
    segment: Segment,
    underlying: str | None,
    itype: InstrumentType = InstrumentType.FUT,
) -> Instrument:
    derivative = itype in (InstrumentType.FUT, InstrumentType.CE, InstrumentType.PE)
    index = itype is InstrumentType.IDX
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=1,
        symbol_name=symbol,
        display_name=symbol,
        instrument=itype,
        segment=segment,
        series=None,
        isin=None,
        underlying_symbol=underlying,
        underlying_security_id=None,
        lot_size=0 if index else 100,
        tick_size=Decimal("0") if index else Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=date(2026, 7, 30) if derivative else None,
        strike_price=Decimal("100") if itype in (InstrumentType.CE, InstrumentType.PE) else None,
        option_type=itype.value if itype in (InstrumentType.CE, InstrumentType.PE) else None,
    )


class TestAmpersandFamily:
    def test_derives_from_fno_legs_deduped_sorted(self) -> None:
        rows = [
            _instrument("M&M-30Jul26-FUT", segment=Segment.FNO, underlying="M&M"),
            _instrument(
                "M&M-30Jul26-2400-CE",
                segment=Segment.FNO,
                underlying="M&M",
                itype=InstrumentType.CE,
            ),
            _instrument("L&TFH-30Jul26-FUT", segment=Segment.FNO, underlying="L&TFH"),
            _instrument("NIFTY-30Jul26-FUT", segment=Segment.FNO, underlying="NIFTY"),
        ]
        assert ampersand_family(rows) == ("L&TFH", "M&M")

    def test_novel_future_listing_is_auto_covered(self) -> None:
        """DYNAMIC pin: a brand-new X&Y F&O listing needs ZERO code change."""
        rows = [_instrument("X&Y-30Jul26-FUT", segment=Segment.FNO, underlying="X&Y")]
        assert ampersand_family(rows) == ("X&Y",)

    def test_non_fno_and_none_underlying_rows_are_excluded(self) -> None:
        rows = [
            _instrument("M&M", segment=Segment.CASH, underlying="M&M", itype=InstrumentType.EQ),
            _instrument("NIFTY", segment=Segment.INDEX, underlying=None, itype=InstrumentType.IDX),
        ]
        assert ampersand_family(rows) == ()
