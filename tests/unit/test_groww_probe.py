"""In-package Groww probe — every verdict branch (DEC-DHAN-GROWW-REMIGRATE-001).

Drives :mod:`brutex.adapters.groww.probe` with a FAKE ``GrowwClient`` (no
``growwapi``, no network) so each check's OK / ABSENT / ERROR verdict and
``run_probe`` are 100% covered.
"""

from __future__ import annotations

from collections.abc import Mapping

from brutex.adapters.groww.errors import GrowwError, GrowwNotFoundError
from brutex.adapters.groww.probe import (
    VERDICT_ABSENT,
    VERDICT_ERROR,
    VERDICT_OK,
    probe_granularity,
    probe_options,
    probe_vix,
    run_probe,
)


class _FakeClient:
    """A fake :class:`GrowwClient` returning canned payloads / raising on demand.

    Each method consults a per-method script: a callable (called with the
    kwargs) returning the payload, or raising. Duck-typed — the probe only calls
    ``historical_candles`` / ``expiries`` / ``contracts``.
    """

    def __init__(
        self,
        *,
        candles: object = None,
        expiries: object = None,
        contracts: object = None,
    ) -> None:
        self._candles = candles
        self._expiries = expiries
        self._contracts = contracts

    def historical_candles(self, **kwargs: object) -> Mapping[str, object]:
        result = self._candles(kwargs) if callable(self._candles) else self._candles
        if isinstance(result, GrowwError):
            raise result
        return result  # type: ignore[return-value]

    def expiries(self, **kwargs: object) -> Mapping[str, object]:
        if isinstance(self._expiries, GrowwError):
            raise self._expiries
        return self._expiries  # type: ignore[return-value]

    def contracts(self, **kwargs: object) -> Mapping[str, object]:
        if isinstance(self._contracts, GrowwError):
            raise self._contracts
        return self._contracts  # type: ignore[return-value]


# ── probe_vix ───────────────────────────────────────────────────────────────


def test_vix_ok_first_candidate() -> None:
    client = _FakeClient(candles={"candles": [[1, 2], [3, 4]]})
    result = probe_vix(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_OK
    assert result.symbol == "NSE-INDIAVIX"
    assert result.rows == 2


def test_vix_ok_second_candidate_after_empty_first() -> None:
    def candles(kwargs: dict[str, object]) -> dict[str, object]:
        if kwargs["groww_symbol"] == "NSE-INDIAVIX":
            return {"candles": []}
        return {"candles": [[1]]}

    client = _FakeClient(candles=candles)
    result = probe_vix(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_OK
    assert result.symbol == "NSE-INDIA VIX"


def test_vix_absent_when_all_empty() -> None:
    client = _FakeClient(candles={"candles": []})
    result = probe_vix(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_ABSENT
    assert result.symbol is None


def test_vix_absent_when_non_dict_payload() -> None:
    client = _FakeClient(candles="not-a-dict")
    result = probe_vix(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_ABSENT


def test_vix_absent_when_candles_not_a_list() -> None:
    # `candles` key present but not a list exercises _candle_rows's non-list branch.
    client = _FakeClient(candles={"candles": "notalist"})
    result = probe_vix(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_ABSENT


def test_vix_error_on_groww_error() -> None:
    client = _FakeClient(candles=GrowwNotFoundError("boom"))
    result = probe_vix(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_ERROR
    assert "India VIX probe failed" in result.detail


# ── probe_granularity ───────────────────────────────────────────────────────


def test_granularity_ok_with_first_ts() -> None:
    client = _FakeClient(candles={"candles": [["2020-01-01 09:15:00", 1, 2]]})
    result = probe_granularity(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_OK
    assert result.rows == 1
    assert "2020-01-01" in result.detail


def test_granularity_ok_scalar_first_row() -> None:
    # A non-list first row exercises the scalar branch of first_ts.
    client = _FakeClient(candles={"candles": ["barstring"]})
    result = probe_granularity(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_OK
    assert "barstring" in result.detail


def test_granularity_ok_empty_first_row() -> None:
    # An empty list first row exercises the `and first` falsy branch.
    client = _FakeClient(candles={"candles": [[]]})
    result = probe_granularity(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_OK


def test_granularity_absent_when_empty() -> None:
    client = _FakeClient(candles={"candles": []})
    result = probe_granularity(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_ABSENT
    assert result.rows == 0


def test_granularity_error() -> None:
    client = _FakeClient(candles=GrowwError("down"))
    result = probe_granularity(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_ERROR
    assert "granularity probe failed" in result.detail


# ── probe_options ───────────────────────────────────────────────────────────


def test_options_ok() -> None:
    client = _FakeClient(
        expiries={"expiries": ["2020-01-30", "2020-02-27"]},
        contracts={"contracts": ["c1", "c2", "c3"]},
    )
    result = probe_options(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_OK
    assert result.rows == 3
    assert "2 expiries" in result.detail


def test_options_ok_non_list_contracts_counts_zero() -> None:
    client = _FakeClient(
        expiries={"expiries": ["2020-01-30"]},
        contracts={"contracts": "nope"},
    )
    result = probe_options(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_OK
    assert result.rows == 0


def test_options_absent_no_expiries() -> None:
    client = _FakeClient(expiries={"expiries": []})
    result = probe_options(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_ABSENT


def test_options_absent_non_dict_expiries() -> None:
    client = _FakeClient(expiries="nope")
    result = probe_options(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_ABSENT


def test_options_error_on_expiries() -> None:
    client = _FakeClient(expiries=GrowwError("exp-boom"))
    result = probe_options(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_ERROR
    assert "get_expiries failed" in result.detail


def test_options_error_on_contracts() -> None:
    client = _FakeClient(
        expiries={"expiries": ["2020-01-30"]},
        contracts=GrowwError("con-boom"),
    )
    result = probe_options(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_ERROR
    assert "get_contracts failed" in result.detail


# ── run_probe ───────────────────────────────────────────────────────────────


def test_run_probe_runs_three_checks() -> None:
    client = _FakeClient(
        candles={"candles": [[1]]},
        expiries={"expiries": ["2020-01-30"]},
        contracts={"contracts": ["c1"]},
    )
    results = run_probe(client)  # type: ignore[arg-type]
    assert [r.name for r in results] == ["vix", "granularity", "options"]
    assert all(r.verdict == VERDICT_OK for r in results)
