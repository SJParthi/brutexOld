"""The ``&``-family (%26) symbol-form probe (DEC-FORM-WINNER-PERSIST-001).

Fakes only — no network. Pins the probe's evidence discipline (BOTH forms
ALWAYS called; one canonical transcript event per physical call BEFORE any
verdict interpretation; budget spent BEFORE each call), every per-underlying
verdict branch (PLAIN_OK / ENCODED_ONLY / BOTH_OK / NEITHER / ERROR), the
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
    AMP_VERDICT_ERROR,
    AMP_VERDICT_MIXED,
    AMP_VERDICT_NEITHER,
    AMP_VERDICT_PLAIN_OK,
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

    def test_data_trumps_a_sibling_error(self) -> None:
        """Plain data + encoded error → PLAIN_OK (data is decisive evidence)."""
        sdk = _AmpSDK(
            expiries={"M&M": "data", "M%26M": "raise"},
            contracts={"M&M": "data", "M%26M": "raise"},
        )
        report, _t = _probe(sdk, ["M&M"])
        assert report.per_underlying == {"M&M": AMP_VERDICT_PLAIN_OK}
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

    def test_non_mapping_payload_counts_zero(self) -> None:
        class _WeirdSDK(_AmpSDK):
            def get_expiries(self, **kw: object) -> Mapping[str, object]:
                self.expiry_forms.append(str(kw["underlying_symbol"]))
                return ["not", "a", "mapping"]  # type: ignore[return-value]

        report, _t = _probe(_WeirdSDK(), ["M&M"])
        assert report.per_underlying == {"M&M": AMP_VERDICT_NEITHER}

    def test_non_array_expiries_value_counts_zero_and_seeds_nothing(self) -> None:
        """A mapping whose ``expiries`` is not an array is a 0-count clean call."""

        class _NonArraySDK(_AmpSDK):
            def get_expiries(self, **kw: object) -> Mapping[str, object]:
                self.expiry_forms.append(str(kw["underlying_symbol"]))
                return {"expiries": "oops-not-an-array"}

        sdk = _NonArraySDK(contracts={"M%26M": "data"})
        report, _t = _probe(sdk, ["M&M"])
        assert sdk.contract_forms == []  # no parseable expiry → contracts leg skipped
        assert report.per_underlying == {"M&M": AMP_VERDICT_NEITHER}

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
