"""Frozen golden corpus for the lake_keys Rust seam (3-arm parity).

DEC-DATA-RUST-LAKE-KEYS-001 (T10 PR-2). Every case runs THREE arms — the
oracle (the ``_*_python`` verbatim laws), the facade flag-OFF
(byte-identical by contract), and the facade flag-ON in the post-burn-in
steady state (``reset_lake_keys_rust_for_tests(remaining=0)``, REAL wheel —
skipped with a visible reason when the ``brutex_data`` wheel is absent;
teeth in the data-rust-parity lane, the ``test_fno_symbol_rust_parity``
precedent) — all equal under exact ``str``/``tuple`` compare, expected
values FROZEN inline.

The corpus transcribes the design §6.1 enumeration end-to-end: the
fleet-required trailing-``0x1F`` golden (#B6) with its ``0x1B`` + ``0x0E``
REJECT twins (#B7), the ß→SS DEGRADE-returns-oracle arms (#A6/#C12 — the
full non-ASCII roster), #B9 as the Rust-ENGAGED byte-blind-prefix case,
the #C6 raw-identity ENGAGEMENT pin (the crate must stay engaged — no
``result_refused_recovery`` refuse, no latch), the alias map both
directions, the lowercase-env-VALUE asymmetry (#C7), the ``"NSE-"``
empty-candidate drop (#B8), the ``%26`` negative pledge pin (#C13), the
dedup/identity laws (#C8/#C9), the structural output ceiling, and the
prefix invariant as a golden (#C15 + the law-composition rows).
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator, Mapping

import pytest
from brutex.data import fno_symbol_rust as fsr
from brutex.data import lake_keys as lk
from brutex.data import lake_keys_rust as lkr
from brutex.data.lake_keys import (
    _spot_key_candidates_python,
    _spot_recovery_candidates_python,
    _stock_drift_fold_python,
    spot_key_candidates,
    spot_recovery_candidates,
    stock_drift_fold,
)
from structlog.testing import capture_logs

_WHEEL_PRESENT = importlib.util.find_spec("brutex_data") is not None

type _DriftMap = Mapping[tuple[str, str], tuple[str, ...]] | None

# --------------------------------------------------------------------------
# Law A — stock_drift_fold (7 cases, design §6.1).
# --------------------------------------------------------------------------
FOLD_CORPUS: tuple[tuple[str, str, str], ...] = (
    ("A1", "BAJAJ-AUTO", "BAJAJAUTO"),
    ("A2", "india vix", "INDIAVIX"),  # interior space folded
    ("A3", " M&M ", "M&M"),  # edge spaces folded (fold removes 0x20 everywhere)
    ("A4", "a b\tc-d", "AB\tCD"),  # tab NOT folded — the two-vocabulary row
    ("A5", "", ""),  # empty legal
    # A6: ß→SS via the ORACLE — the DEGRADE-returns-oracle pin, both arms
    # (non-ASCII input closes the gate; the length-changing Unicode upper).
    ("A6", "straße-1", "STRASSE1"),
    ("A7", "m&m", "M&M"),  # '&' untouched
)

# --------------------------------------------------------------------------
# Law B — spot_key_candidates (11 groups, design §6.1).
# --------------------------------------------------------------------------
CANDIDATES_CORPUS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("B1", "M&M ", "NSE", ("M&M",)),  # whitespace rung
    ("B2", "NSE-M&M", "NSE", ("M&M",)),  # prefix rung on a clean symbol
    ("B3", " NSE-M&M", "NSE", ("NSE-M&M", "M&M")),  # both rungs, order pinned
    ("B4", "BAJAJ-AUTO", "NSE", ()),  # hyphenated never mangled; clean = no candidates
    ("B5", "\x1cNSE-M&M\x1c", "NSE", ("NSE-M&M", "M&M")),  # strip-set accept twin
    # B6: the fleet-required trailing-0x1F golden.
    ("B6", "NSE-M&M\x1f", "NSE", ("NSE-M&M", "M&M")),
    # B7: 0x1B REJECT twin (ESC not in the strip set — the prefix no longer
    # anchors) + the 0x0E lower-edge REJECT twin (SO, first byte past the
    # 0x09–0x0D run, symmetric to 0x1B).
    ("B7a", "\x1bNSE-M&M", "NSE", ()),
    ("B7b", "\x0eNSE-M&M", "NSE", ()),
    # B8: the pathological empty-candidate drop, both shapes.
    ("B8a", "NSE-", "NSE", ()),
    ("B8b", " NSE- ", "NSE", ("NSE-",)),
    # B9: Rust-ENGAGED byte-blind-prefix case — "NSE-FI".isascii() is True,
    # so the gate does NOT close; flag-ON ships the CRATE value, value-equal
    # to the oracle (prefix strip is byte-blind, "FI" never case-mapped).
    ("B9", "NSE-FI", "NSE", ("FI",)),
    ("B10a", "", "NSE", ()),
    ("B10b", "  ", "NSE", ()),  # whitespace-only strips to "" → rung-1 guard
    ("B11a", "BSE-M&M", "NSE", ()),  # foreign prefix not a candidate
    ("B11b", "NSE-M&M", " NSE ", ("M&M",)),  # exchange stripped before prefixing
)

# --------------------------------------------------------------------------
# Law C — spot_recovery_candidates (15 groups + 3 law-composition rows).
# Each row: (case id, underlying, exchange, env payload (None = unset),
# stock_drift map (None = omitted), frozen expected tuple).
# --------------------------------------------------------------------------
RECOVERY_CORPUS: tuple[tuple[str, str, str, str | None, _DriftMap, tuple[str, ...]], ...] = (
    # C1/C2: sourced alias pair, both directions.
    ("C1", "NIFTYNXT50", "NSE", None, None, ("NIFTYJR", "NSE-NIFTYNXT50")),
    ("C2", "NIFTYJR", "NSE", None, None, ("NIFTYNXT50", "NSE-NIFTYJR")),
    # C3: the second sourced pair, both directions.
    ("C3a", "MIDCPNIFTY", "NSE", None, None, ("NIFTYMIDSELECT", "NSE-MIDCPNIFTY")),
    ("C3b", "NIFTYMIDSELECT", "NSE", None, None, ("MIDCPNIFTY", "NSE-NIFTYMIDSELECT")),
    # C4: env-override pair (key uppercased at parse), both directions.
    ("C4a", "TATACS", "NSE", '{"tcs": "TATACS"}', None, ("TCS", "NSE-TATACS")),
    ("C4b", "TCS", "NSE", '{"tcs": "TATACS"}', None, ("TATACS", "NSE-TCS")),
    # C5: malformed env ignored → sourced-only ladder.
    ("C5a", "NIFTYJR", "NSE", "not json", None, ("NIFTYNXT50", "NSE-NIFTYJR")),
    ("C5b", "NIFTYJR", "NSE", "[1]", None, ("NIFTYNXT50", "NSE-NIFTYJR")),
    ("C5c", "NIFTYJR", "NSE", '{"a": 1}', None, ("NIFTYNXT50", "NSE-NIFTYJR")),
    # C6: the raw-C6-vs-stripped-C2 mix frozen in order (the ON arm carries
    # the ENGAGEMENT pin separately — test_c6_engagement_pin below).
    (
        "C6",
        " NIFTYNXT50 ",
        "NSE",
        None,
        None,
        ("NIFTYJR", "NSE- NIFTYNXT50 ", "NIFTYNXT50"),
    ),
    # C7: the lowercase env VALUE asymmetry — C4 upper compare emits the
    # KEY; C5 verbatim compare emits the LOWERCASE value.
    ("C7a", "BAR", "NSE", '{"foo": "bar"}', None, ("FOO", "NSE-BAR")),
    ("C7b", "FOO", "NSE", '{"foo": "bar"}', None, ("bar", "NSE-FOO")),
    # C8: dedup first-occurrence — the alias occurrence wins position 0,
    # the bucket duplicate vanishes, the second bucket entry appends last.
    (
        "C8",
        "BAJAJAUTO",
        "NSE",
        '{"BAJAJ-AUTO": "BAJAJAUTO"}',
        {("NSE", "BAJAJAUTO"): ("BAJAJ-AUTO", "BAJAJ AUTO")},
        ("BAJAJ-AUTO", "NSE-BAJAJAUTO", "BAJAJ AUTO"),
    ),
    # C9: identity exclusion — the identity bucket entry excluded, the
    # drifted one appended.
    (
        "C9",
        "M&M",
        "NSE",
        None,
        {("NSE", "M&M"): ("M&M", "M&M ")},
        ("NSE-M&M", "M&M "),
    ),
    # C10: blank exchange — C6 is UNCONDITIONAL (raw format survives a blank
    # exchange; the law-B prefix rung is skipped).
    ("C10", "Y", "", None, None, ("-Y",)),
    # C11: degenerate empty underlying — C6 of "" ; law B on "" → ().
    ("C11", "", "NSE", None, None, ("NSE-",)),
    # C12: non-ASCII routed to the oracle on BOTH arms (the gate pin at the
    # golden level) — a non-ASCII underlying AND a non-ASCII BUCKET entry
    # (the gate sweeps the bucket too).
    ("C12a", "nse-ß", "NSE", None, None, ("NSE-nse-ß",)),
    ("C12b", "MUM", "NSE", None, {("NSE", "MUM"): ("MÜM",)}, ("NSE-MUM", "MÜM")),
    # C13: the %26 negative pledge pin — no '&'-carrying candidate on ANY
    # arm (asserted structurally in the check helper).
    ("C13", "M%26M", "NSE", None, None, ("NSE-M%26M",)),
    # C14: drifted-SHAPED underlying (rung 3) walks back down.
    ("C14", "NSE-M&M", "NSE", None, None, ("NSE-NSE-M&M", "M&M")),
    # C15: the prefix invariant as a golden — C8's inputs with NO bucket
    # equal the first 2 entries of the with-bucket answer (byte prefix).
    (
        "C15",
        "BAJAJAUTO",
        "NSE",
        '{"BAJAJ-AUTO": "BAJAJAUTO"}',
        None,
        ("BAJAJ-AUTO", "NSE-BAJAJAUTO"),
    ),
    # Law-composition rows (design §6.1 tail): the PUBLIC entry with a real
    # stock_drift dict — ordered multi-entry bucket (master-exact first).
    (
        "D1",
        "BAJAJAUTO",
        "NSE",
        None,
        {("NSE", "BAJAJAUTO"): ("BAJAJ-AUTO", "BAJAJ AUTO")},
        ("NSE-BAJAJAUTO", "BAJAJ-AUTO", "BAJAJ AUTO"),
    ),
    # D2: stock_drift=None ≡ {} ≡ absent-key byte-identity (the ambiguous-
    # fold poison shape is EXCLUDED at spot_drift map build, so from
    # lake_keys' view it is an absent key → empty bucket → pre-stock ladder).
    ("D2a", "NIFTYJR", "NSE", None, None, ("NIFTYNXT50", "NSE-NIFTYJR")),
    ("D2b", "NIFTYJR", "NSE", None, {}, ("NIFTYNXT50", "NSE-NIFTYJR")),
    (
        "D2c",
        "NIFTYJR",
        "NSE",
        None,
        {("NSE", "OTHER"): ("X",)},
        ("NIFTYNXT50", "NSE-NIFTYJR"),
    ),
    # D3: fold-to-empty underlying — " - " folds to "" so the bucket is
    # NEVER probed (the wrapper's ``if fold:`` guard); the poison entry
    # keyed at ("NSE", "") must not appear.
    ("D3", " - ", "NSE", None, {("NSE", ""): ("POISON",)}, ("NSE- - ", "-")),
)

_FOLD_IDS = [case_id for case_id, _, _ in FOLD_CORPUS]
_CAND_IDS = [case_id for case_id, _, _, _ in CANDIDATES_CORPUS]
_REC_IDS = [case_id for case_id, _, _, _, _, _ in RECOVERY_CORPUS]


def _apply_alias_env(monkeypatch: pytest.MonkeyPatch, env: str | None) -> None:
    if env is None:
        monkeypatch.delenv(lk.FNO_INDEX_ALIASES_ENV, raising=False)
    else:
        monkeypatch.setenv(lk.FNO_INDEX_ALIASES_ENV, env)


def _check_recovery_result(
    case_id: str, underlying: str, drift: _DriftMap, result: tuple[str, ...]
) -> None:
    """Shared structural laws for every recovery arm's result."""
    # Identity exclusion — the RAW underlying param never echoes.
    assert underlying not in result
    # Dedup — first occurrence wins, no duplicates survive.
    assert len(result) == len(set(result))
    # The structural output ceiling: ≤2 adds per alias pair + C6 + ≤2
    # law-B rungs + ≤ the bucket length.
    bucket_len = max((len(v) for v in drift.values()), default=0) if drift else 0
    ceiling = 2 * len(lk.fno_index_alias_pairs()) + 3 + bucket_len
    assert len(result) <= ceiling
    if case_id == "C13":
        # The %26 negative pledge: no '&'-carrying candidate ever minted.
        assert all("&" not in entry for entry in result)
    if case_id == "D3":
        assert "POISON" not in result


class TestOracleArm:
    """Arm 1: the extracted verbatim Python laws against the frozen values."""

    @pytest.mark.parametrize(("case_id", "symbol", "expected"), FOLD_CORPUS, ids=_FOLD_IDS)
    def test_fold(self, case_id: str, symbol: str, expected: str) -> None:
        assert _stock_drift_fold_python(symbol) == expected

    @pytest.mark.parametrize(
        ("case_id", "symbol", "exchange", "expected"), CANDIDATES_CORPUS, ids=_CAND_IDS
    )
    def test_candidates(
        self, case_id: str, symbol: str, exchange: str, expected: tuple[str, ...]
    ) -> None:
        assert _spot_key_candidates_python(symbol, exchange) == expected

    @pytest.mark.parametrize(
        ("case_id", "underlying", "exchange", "env", "drift", "expected"),
        RECOVERY_CORPUS,
        ids=_REC_IDS,
    )
    def test_recovery(
        self,
        monkeypatch: pytest.MonkeyPatch,
        case_id: str,
        underlying: str,
        exchange: str,
        env: str | None,
        drift: _DriftMap,
        expected: tuple[str, ...],
    ) -> None:
        _apply_alias_env(monkeypatch, env)
        pairs = lk.fno_index_alias_pairs()
        bucket: tuple[str, ...] = ()
        if drift:
            fold = _stock_drift_fold_python(underlying)
            if fold:
                bucket = tuple(drift.get((exchange, fold), ()))
        result = _spot_recovery_candidates_python(
            underlying, exchange, alias_pairs=pairs, drift_bucket=bucket
        )
        assert result == expected
        _check_recovery_result(case_id, underlying, drift, result)


class _FlagArmBase:
    """Shared parametrized bodies for the two routed (public-entry) arms."""

    @pytest.mark.parametrize(("case_id", "symbol", "expected"), FOLD_CORPUS, ids=_FOLD_IDS)
    def test_fold(self, case_id: str, symbol: str, expected: str) -> None:
        assert stock_drift_fold(symbol) == expected

    @pytest.mark.parametrize(
        ("case_id", "symbol", "exchange", "expected"), CANDIDATES_CORPUS, ids=_CAND_IDS
    )
    def test_candidates(
        self, case_id: str, symbol: str, exchange: str, expected: tuple[str, ...]
    ) -> None:
        assert spot_key_candidates(symbol, exchange) == expected

    @pytest.mark.parametrize(
        ("case_id", "underlying", "exchange", "env", "drift", "expected"),
        RECOVERY_CORPUS,
        ids=_REC_IDS,
    )
    def test_recovery(
        self,
        monkeypatch: pytest.MonkeyPatch,
        case_id: str,
        underlying: str,
        exchange: str,
        env: str | None,
        drift: _DriftMap,
        expected: tuple[str, ...],
    ) -> None:
        _apply_alias_env(monkeypatch, env)
        if drift is None:
            result = spot_recovery_candidates(underlying, exchange)
        else:
            result = spot_recovery_candidates(underlying, exchange, stock_drift=drift)
        assert result == expected
        _check_recovery_result(case_id, underlying, drift, result)

    def test_prefix_invariant_c8_vs_c15(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """#C15: the no-bucket ladder is a byte PREFIX of the with-bucket one."""
        _apply_alias_env(monkeypatch, '{"BAJAJ-AUTO": "BAJAJAUTO"}')
        without = spot_recovery_candidates("BAJAJAUTO", "NSE")
        with_bucket = spot_recovery_candidates(
            "BAJAJAUTO",
            "NSE",
            stock_drift={("NSE", "BAJAJAUTO"): ("BAJAJ-AUTO", "BAJAJ AUTO")},
        )
        assert with_bucket[: len(without)] == without
        assert without == ("BAJAJ-AUTO", "NSE-BAJAJAUTO")


class TestFlagOffArm(_FlagArmBase):
    """Arm 2: the routed public entries, flag unset — byte-identical oracle."""

    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        monkeypatch.delenv(fsr.DATA_RUST_ENV, raising=False)
        lkr.reset_lake_keys_rust_for_tests()
        yield
        lkr.reset_lake_keys_rust_for_tests()


@pytest.mark.skipif(
    not _WHEEL_PRESENT,
    reason="brutex_data wheel not installed — the data-rust-parity lane builds it (teeth there)",
)
class TestFlagOnArm(_FlagArmBase):
    """Arm 3: the routed public entries, flag ON, REAL wheel, steady state."""

    @pytest.fixture(autouse=True)
    def _engaged(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        monkeypatch.setenv(fsr.DATA_RUST_ENV, "1")
        lkr.reset_lake_keys_rust_for_tests(remaining=0)  # post-burn-in steady state
        yield
        # Anti-vacuous: no guard latched the seam OFF mid-case (an engaged
        # run must stay engaged — a latch would silently demote every later
        # assertion to the oracle arm). The A6/C12 non-ASCII cases route to
        # the oracle through the GATE, which never latches.
        latched = lkr.lake_keys_rust_disabled()
        lkr.reset_lake_keys_rust_for_tests()
        assert latched is False

    def test_b9_ships_engaged(self) -> None:
        """#B9: ASCII input — the crate value ships, byte-blind prefix strip."""
        assert spot_key_candidates("NSE-FI", "NSE") == ("FI",)
        assert lkr.lake_keys_rust_disabled() is False

    def test_c6_engagement_pin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """#C6 ENGAGEMENT pin (R1 CORR MINOR-1).

        ``"NIFTYNXT50"`` equals the STRIPPED underlying, so a
        raw-identity-violating (normalizing) refuse gate would silently
        oracle-fall-back forever — the value-only assertion alone would
        pass (oracle == frozen). Pin: the crate stays engaged AND no
        ``result_refused_recovery`` warn fired.
        """
        _apply_alias_env(monkeypatch, None)
        with capture_logs() as logs:
            result = spot_recovery_candidates(" NIFTYNXT50 ", "NSE")
        assert result == ("NIFTYJR", "NSE- NIFTYNXT50 ", "NIFTYNXT50")
        assert lkr.lake_keys_rust_disabled() is False
        refused = [
            entry
            for entry in logs
            if entry.get("reason") == "result_refused_recovery"
            or "result_refused_recovery" in str(entry.get("event", ""))
        ]
        assert refused == []

    def test_engagement_is_genuine(self) -> None:
        """The engaged INFO names the real crate version — proof the corpus
        exercised Rust, not a silently-latched oracle path."""
        with capture_logs() as logs:
            assert stock_drift_fold("BAJAJ-AUTO") == "BAJAJAUTO"
        engaged = [e for e in logs if e["event"] == "lake_keys_rust.engaged"]
        assert len(engaged) == 1
        assert engaged[0]["crate_version"]

    def test_burn_in_arm_also_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A full-burn-in pass over all three corpora completes with ZERO
        autoreverts — the oracle-compare belt agrees with Rust."""
        lkr.reset_lake_keys_rust_for_tests()  # remaining = BURN_IN_SAMPLES
        for _, symbol, _ in FOLD_CORPUS:
            stock_drift_fold(symbol)
        for _, symbol, exchange, _ in CANDIDATES_CORPUS:
            spot_key_candidates(symbol, exchange)
        for _, underlying, exchange, env, drift, _ in RECOVERY_CORPUS:
            _apply_alias_env(monkeypatch, env)
            if drift is None:
                spot_recovery_candidates(underlying, exchange)
            else:
                spot_recovery_candidates(underlying, exchange, stock_drift=drift)
        assert lkr.lake_keys_rust_disabled() is False
