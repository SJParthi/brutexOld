"""Tests for :mod:`brutex.adapters.dhan.constants` — verified values + honesty pins."""

from __future__ import annotations

import ast
from pathlib import Path

from brutex.adapters.dhan import constants


def test_verified_endpoint_and_addressing() -> None:
    assert constants.API_BASE == "https://api.dhan.co/v2"
    assert constants.SEGMENT_INDEX == "IDX_I"
    assert constants.INSTRUMENT_INDEX == "INDEX"
    assert constants.DHAN_INTERVAL_1MIN == 1
    assert constants.DHAN_FETCH_INTERVAL == "1minute"
    assert constants.NATIVE_TIMEZONE == "Asia/Kolkata"


def test_window_and_rate_limits() -> None:
    assert constants.DHAN_INTRADAY_WINDOW_DAYS == 90
    assert constants.DHAN_RATE_LIMIT_RPS == 5.0
    assert constants.DHAN_DAILY_QUOTA == 100_000
    assert constants.DHAN_RATE_LIMIT_PER_MIN is None  # honestly none, not a fabricated cap


def test_ledger_namespace_and_feed_id() -> None:
    assert constants.DHAN_COVERAGE_MARKET == "dhan"
    assert constants.DHAN_FEED_ID == "dhan"


def test_index_symbol_sets() -> None:
    assert constants.DHAN_ENGINE_INDEX_SYMBOLS == ("NIFTY", "BANKNIFTY", "SENSEX")
    # INDIA VIX is REFERENCE_ONLY — present in the spot set, absent from the engine set.
    assert "INDIAVIX" in constants.DHAN_SPOT_INDEX_SYMBOLS
    assert "INDIAVIX" not in constants.DHAN_ENGINE_INDEX_SYMBOLS


def test_index_security_ids_are_the_in_repo_verified_values() -> None:
    assert constants.DHAN_INDEX_SECURITY_IDS == {
        "NIFTY": 13,
        "BANKNIFTY": 25,
        "SENSEX": 51,
        "INDIAVIX": 21,  # NOT 26 (a known third-party mirror error)
    }


def test_error_codes() -> None:
    assert constants.DHAN_ERR_INVALID_AUTH == "DH-901"
    # RELABEL (design §2, A-MINOR-2): the SUBSCRIPTION/authorization class is
    # DH-902 (the annexure's "User has not subscribed to Data APIs…"); DH-905 is
    # the INPUT exception (a bad request), carried by its own constant.
    assert constants.DHAN_ERR_AUTHORIZATION == "DH-902"
    assert constants.DHAN_ERR_INPUT_EXCEPTION == "DH-905"
    assert constants.DHAN_ERR_RATE_LIMIT == "DH-904"
    assert constants.DHAN_ERR_DATA_FETCH == "DH-907"
    assert constants.DHAN_ERR_INTERNAL_SERVER == "DH-908"
    assert constants.DHAN_ERR_NETWORK == "DH-909"


def test_numeric_data_api_error_codes() -> None:
    # The Data-API numeric code table (mirror 20-annexure.md) as named constants.
    assert constants.DHAN_ERR_NUM_INTERNAL == "800"
    assert constants.DHAN_ERR_NUM_INSTRUMENT_LIMIT == "804"
    assert constants.DHAN_ERR_NUM_RATE_LIMIT == "805"
    assert constants.DHAN_ERR_NUM_NOT_SUBSCRIBED == "806"
    assert constants.DHAN_ERR_NUM_TOKEN_EXPIRED == "807"
    assert constants.DHAN_ERR_NUM_AUTH_FAILED == "808"
    assert constants.DHAN_ERR_NUM_TOKEN_INVALID == "809"
    assert constants.DHAN_ERR_NUM_CLIENT_ID_INVALID == "810"
    assert constants.DHAN_ERR_NUM_INVALID_EXPIRY == "811"
    assert constants.DHAN_ERR_NUM_INVALID_DATE == "812"
    assert constants.DHAN_ERR_NUM_INVALID_SECURITY_ID == "813"
    assert constants.DHAN_ERR_NUM_INVALID_REQUEST == "814"


def test_ssm_parameter_paths() -> None:
    assert constants.DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT == "/tickvault/prod/dhan/client-id"
    assert constants.DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT == "/tickvault/prod/dhan/access-token"
    assert constants.DHAN_TOKEN_PARAM_CLIENT_ID_ENV == "BRUTEX_TOKEN_PARAM_DHAN_CLIENT_ID"
    assert constants.DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV == "BRUTEX_TOKEN_PARAM_DHAN_ACCESS_TOKEN"


def test_history_floor_is_none_until_verified() -> None:
    # Golden Rule #1: the floor is UNVERIFIED — NEVER a fabricated date until a probe.
    assert constants.DHAN_HISTORY_FLOOR is None


def test_servability_sentinels_are_none() -> None:
    assert constants.NIFTY_SERVABILITY_UNVERIFIED is None
    assert constants.BANKNIFTY_SERVABILITY_UNVERIFIED is None
    assert constants.SENSEX_SERVABILITY_UNVERIFIED is None
    assert constants.INDIAVIX_SERVABILITY_UNVERIFIED is None


_UNVERIFIED_SENTINEL_NAMES = frozenset(
    {
        "NIFTY_SERVABILITY_UNVERIFIED",
        "BANKNIFTY_SERVABILITY_UNVERIFIED",
        "SENSEX_SERVABILITY_UNVERIFIED",
        "INDIAVIX_SERVABILITY_UNVERIFIED",
    }
)


def test_no_decision_branch_imports_the_unverified_sentinels() -> None:
    """AST pin: no dhan module (other than constants) references a servability sentinel.

    The sentinels are HONESTY MARKERS ONLY — the pull must never gate on a fabricated
    "verified" flag (the Groww ``_UNVERIFIED`` pattern). This scans every ``.py`` file
    in the dhan package (excluding ``constants.py`` where they are defined) PLUS the
    Dhan surfaces that live OUTSIDE the package — the dashboard pull glue, the feed
    binding, and the read-only credentials reader — so the "no decision branch reads a
    sentinel" guarantee matches the honesty claim across every Dhan consumer.
    """
    import brutex.config.credentials_dhan_aws as _creds_mod
    import brutex.dashboard.dhan_pull as _pull_mod
    import brutex.feeds.dhan_binding as _binding_mod

    dhan_pkg = Path(constants.__file__).parent
    source_files = [f for f in sorted(dhan_pkg.glob("*.py")) if f.name != "constants.py"]
    source_files += [
        Path(_pull_mod.__file__),
        Path(_binding_mod.__file__),
        Path(_creds_mod.__file__),
    ]
    for source_file in source_files:
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        referenced = (names | attrs) & _UNVERIFIED_SENTINEL_NAMES
        assert not referenced, f"{source_file.name} references sentinel(s) {referenced}"
