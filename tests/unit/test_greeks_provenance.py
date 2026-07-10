"""Unit tests for :mod:`brutex.data.greeks_provenance` (DEC-DB-FNO-DISPLAY-TRUTH-001 item 3).

The COMMON wheel-free home of the packed ``greeks_provenance_id`` vocabulary,
MOVED VERBATIM from ``greeks_enrich.py`` (the #1581 re-export precedent). Pins:

* pack → unpack round-trip over every status/style, byte-identical to the
  pre-move behaviour (the lazy ``rate_version`` default resolves to the SAME
  ``RISK_FREE_SOURCE_VERSION`` single source of truth);
* the ``greeks_enrich`` re-exports are the SAME objects (identity, not copies);
* ``greeks_status_label`` — all seven statuses, the ``None``-stamp option, the
  future, and the honest unknown-status fallback;
* the module imports WITHOUT the ``trading_core`` wheel chain (the viewer
  read path's wheel-free requirement).
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from brutex.data.greeks_provenance import (
    GREEKS_FORMULA_VERSION,
    INPUTS_MASK,
    Q_BIAS_ZERO,
    RATE_SOURCE_ID_CSV,
    RATE_SOURCE_ID_FIXED,
    RATE_SOURCE_ID_NONE,
    STATUS_AT_EXPIRY,
    STATUS_BAD_INPUT,
    STATUS_BELOW_INTRINSIC,
    STATUS_DISPLAY_LABELS,
    STATUS_NAMES,
    STATUS_NO_RATE,
    STATUS_NO_SOLUTION,
    STATUS_NO_SPOT,
    STATUS_OK,
    STYLE_AMERICAN_APPROX,
    STYLE_EUROPEAN_EXACT,
    STYLE_UNKNOWN,
    TTE_ACT365_EXPIRY_1530_IST,
    GreeksProvenance,
    greeks_status_label,
    pack_greeks_provenance,
    unpack_greeks_provenance,
)

_ALL_STATUSES = (
    STATUS_OK,
    STATUS_NO_SPOT,
    STATUS_NO_SOLUTION,
    STATUS_AT_EXPIRY,
    STATUS_NO_RATE,
    STATUS_BAD_INPUT,
    STATUS_BELOW_INTRINSIC,
)


class TestPackUnpackRoundTrip:
    @pytest.mark.parametrize("status", _ALL_STATUSES)
    @pytest.mark.parametrize("style", [STYLE_UNKNOWN, STYLE_EUROPEAN_EXACT, STYLE_AMERICAN_APPROX])
    def test_round_trip(self, status: int, style: int) -> None:
        packed = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_CSV, rate_version=3, style_flag=style, status=status
        )
        assert unpack_greeks_provenance(packed) == GreeksProvenance(
            formula_version=GREEKS_FORMULA_VERSION,
            rate_source_id=RATE_SOURCE_ID_CSV,
            rate_version=3,
            q_bias_id=Q_BIAS_ZERO,
            tte_convention_id=TTE_ACT365_EXPIRY_1530_IST,
            style_flag=style,
            status=status,
        )

    def test_lazy_rate_version_default_is_the_risk_free_single_source(self) -> None:
        # The ONE documented deviation from the verbatim move: the default is a
        # None sentinel resolved LAZILY from RISK_FREE_SOURCE_VERSION (never a
        # copied value) — a default-relying call packs the byte-identical int.
        from brutex.options.risk_free import RISK_FREE_SOURCE_VERSION

        defaulted = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_FIXED, style_flag=STYLE_EUROPEAN_EXACT, status=STATUS_OK
        )
        explicit = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_FIXED,
            rate_version=RISK_FREE_SOURCE_VERSION,
            style_flag=STYLE_EUROPEAN_EXACT,
            status=STATUS_OK,
        )
        assert defaulted == explicit
        assert unpack_greeks_provenance(defaulted).rate_version == RISK_FREE_SOURCE_VERSION

    def test_inputs_mask_strips_style_and_status_only(self) -> None:
        a = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_CSV,
            rate_version=2,
            style_flag=STYLE_EUROPEAN_EXACT,
            status=STATUS_OK,
        )
        b = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_CSV,
            rate_version=2,
            style_flag=STYLE_AMERICAN_APPROX,
            status=STATUS_BELOW_INTRINSIC,
        )
        assert a != b
        assert (a & INPUTS_MASK) == (b & INPUTS_MASK)

    @pytest.mark.parametrize(
        "field",
        [
            "formula_version",
            "rate_source_id",
            "rate_version",
            "q_bias_id",
            "tte_convention_id",
            "style_flag",
            "status",
        ],
    )
    @pytest.mark.parametrize("bad", [-1, 1 << 28])
    def test_out_of_band_field_raises_naming_it(self, field: str, bad: int) -> None:
        kwargs: dict[str, int] = {
            "rate_source_id": RATE_SOURCE_ID_NONE,
            "rate_version": 1,
            "style_flag": STYLE_UNKNOWN,
            "status": STATUS_OK,
            "formula_version": GREEKS_FORMULA_VERSION,
            "q_bias_id": Q_BIAS_ZERO,
            "tte_convention_id": TTE_ACT365_EXPIRY_1530_IST,
        }
        kwargs[field] = bad
        with pytest.raises(ValueError, match=field):
            pack_greeks_provenance(**kwargs)


class TestReExportIdentity:
    def test_greeks_enrich_re_exports_the_same_objects(self) -> None:
        # The #1581 re-export precedent: byte-compatible aliases to the SAME
        # objects, so the existing greeks_enrich suites pass UNMODIFIED.
        ge = pytest.importorskip(
            "brutex.data.greeks_enrich", reason="greeks_enrich needs the trading_core wheel"
        )
        gp = sys.modules["brutex.data.greeks_provenance"]
        assert ge.unpack_greeks_provenance is gp.unpack_greeks_provenance
        assert ge.pack_greeks_provenance is gp.pack_greeks_provenance
        assert ge.GreeksProvenance is gp.GreeksProvenance
        assert ge.GREEKS_FORMULA_VERSION == gp.GREEKS_FORMULA_VERSION
        assert ge.STATUS_BELOW_INTRINSIC == gp.STATUS_BELOW_INTRINSIC
        # The two private spellings the enrichment body uses alias the moved names.
        assert ge._STATUS_NAMES is gp.STATUS_NAMES
        assert ge._INPUTS_MASK == gp.INPUTS_MASK


class TestStatusVocabulary:
    def test_display_labels_cover_exactly_the_status_vocabulary(self) -> None:
        assert set(STATUS_DISPLAY_LABELS) == set(STATUS_NAMES) == set(_ALL_STATUSES)

    def test_machine_names_are_the_ctr6_telemetry_labels(self) -> None:
        assert STATUS_NAMES[STATUS_OK] == "ok"
        assert STATUS_NAMES[STATUS_BELOW_INTRINSIC] == "below_intrinsic"


class TestGreeksStatusLabel:
    @pytest.mark.parametrize(
        ("status", "words"),
        [
            (STATUS_OK, "ok"),
            (STATUS_NO_SPOT, "no spot — underlying bar missing"),
            (STATUS_NO_SOLUTION, "no IV solution"),
            (STATUS_AT_EXPIRY, "at/after expiry"),
            (STATUS_NO_RATE, "no risk-free rate"),
            (STATUS_BAD_INPUT, "bad input bar"),
            (STATUS_BELOW_INTRINSIC, "below intrinsic — no unique IV"),
        ],
    )
    def test_every_stamped_status_reads_its_words(self, status: int, words: str) -> None:
        packed = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_FIXED,
            rate_version=1,
            style_flag=STYLE_EUROPEAN_EXACT,
            status=status,
        )
        assert greeks_status_label(packed, is_option=True) == words

    def test_none_stamp_on_an_option_reads_not_enriched(self) -> None:
        assert greeks_status_label(None, is_option=True) == "not enriched"

    def test_a_future_reads_na_even_with_a_stamp(self) -> None:
        packed = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_FIXED,
            rate_version=1,
            style_flag=STYLE_UNKNOWN,
            status=STATUS_OK,
        )
        assert greeks_status_label(None, is_option=False) == "—"
        assert greeks_status_label(packed, is_option=False) == "—"

    def test_unknown_future_status_reads_the_honest_fallback(self) -> None:
        # A future status id this build does not know renders "status <n>" —
        # never a wrong label (Golden Rule #1).
        packed = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_FIXED,
            rate_version=1,
            style_flag=STYLE_UNKNOWN,
            status=15,  # in-band (4 bits) but outside today's vocabulary
        )
        assert greeks_status_label(packed, is_option=True) == "status 15"


class TestWheelFreeImport:
    def test_import_pulls_neither_the_wheel_nor_the_options_package(self) -> None:
        # The viewer read path's requirement: importing + DECODING through this
        # module must not touch trading_core / brutex.options (the lazy pack
        # default is the one documented exception, never reached by a decode).
        code = (
            "import sys\n"
            "import brutex.data.greeks_provenance as gp\n"
            "assert 'trading_core' not in sys.modules, 'wheel leaked'\n"
            "assert 'brutex.options' not in sys.modules, 'options pkg leaked'\n"
            "assert gp.greeks_status_label(None, is_option=True) == 'not enriched'\n"
            "assert 'brutex.options' not in sys.modules\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=False
        )
        assert proc.returncode == 0, proc.stderr
