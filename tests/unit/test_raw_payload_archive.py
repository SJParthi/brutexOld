"""Unit + property + golden tests for ``brutex.data.raw_payload_archive``.

DEC-RAW-PAYLOAD-ARCHIVE-001. 100% line+branch of the verbatim raw-payload
archive (the offline-re-verify extension of the raw-data integrity layer):

* archive → read-back round-trip + the offline-verify MATCH path,
* property: archive→read-back→hash matches the integrity record's hash; a
  1-byte tamper in the archived file is detected as MISMATCH,
* golden: the deterministic compressed key path + the deterministic gzip bytes,
* the DISABLED-by-default no-op hook path (writes nothing),
* the best-effort hook swallow (malformed payload + OSError),
* MISSING (never-archived) + corrupt-gzip-read inconclusive paths.

The candle lake is never touched (the archive lives in its own directory).
"""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path
from typing import Any, TypedDict

import pytest
from brutex.data.raw_integrity import (
    RawIntegrityError,
    RawIntegrityRecord,
    build_record,
    canonicalize_payload,
    hash_payload,
)
from brutex.data.raw_payload_archive import (
    ARCHIVE_DIRNAME,
    ARCHIVE_ENV_FLAG,
    ARCHIVE_SUFFIX,
    ArchiveVerifyOutcome,
    RawPayloadArchiveConfig,
    archive_dir,
    archive_key,
    archive_path,
    archive_payload,
    make_archive_hook,
    make_archive_hook_factory,
    read_archived_canonical_bytes,
    verify_archived_record,
)
from hypothesis import given, settings
from hypothesis import strategies as st

_PAYLOAD: dict[str, Any] = {
    "candles": [["2024-01-01T09:15:00", 100.0, 110.0, 90.0, 105.0, 777, 42]]
}


class _Coords(TypedDict):
    exchange: str
    segment: str
    groww_symbol: str
    interval: str
    window_start: str
    window_end: str


_COORDS: _Coords = {
    "exchange": "NSE",
    "segment": "CASH",
    "groww_symbol": "NSE-NIFTY",
    "interval": "1minute",
    "window_start": "2024-01-01 00:00:00",
    "window_end": "2024-01-31 00:00:00",
}


def _enabled(root: Path) -> RawPayloadArchiveConfig:
    return RawPayloadArchiveConfig(root=root, enabled=True)


def _record() -> RawIntegrityRecord:
    return build_record(_PAYLOAD, **_COORDS)


# --------------------------------------------------------------------------- #
# Config + env
# --------------------------------------------------------------------------- #
def test_config_default_disabled(tmp_path: Path) -> None:
    assert RawPayloadArchiveConfig(root=tmp_path).enabled is False


def test_from_env_default_disabled(tmp_path: Path) -> None:
    cfg = RawPayloadArchiveConfig.from_env(tmp_path, environ={})
    assert cfg.enabled is False
    assert cfg.root == tmp_path


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", " on "])
def test_from_env_truthy_enables(tmp_path: Path, value: str) -> None:
    cfg = RawPayloadArchiveConfig.from_env(tmp_path, environ={ARCHIVE_ENV_FLAG: value})
    assert cfg.enabled is True


@pytest.mark.parametrize("value", ["0", "false", "no", "", "off", "garbage"])
def test_from_env_falsey_stays_disabled(tmp_path: Path, value: str) -> None:
    cfg = RawPayloadArchiveConfig.from_env(tmp_path, environ={ARCHIVE_ENV_FLAG: value})
    assert cfg.enabled is False


def test_from_env_reads_os_environ_when_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ARCHIVE_ENV_FLAG, "1")
    assert RawPayloadArchiveConfig.from_env(tmp_path).enabled is True


# --------------------------------------------------------------------------- #
# Key + path determinism (golden)
# --------------------------------------------------------------------------- #
def test_archive_dir_not_under_bars(tmp_path: Path) -> None:
    cfg = _enabled(tmp_path)
    assert archive_dir(cfg) == tmp_path / ARCHIVE_DIRNAME
    assert "bars" not in archive_path(cfg, **_COORDS).parts
    assert "options" not in archive_path(cfg, **_COORDS).parts


def test_archive_key_golden_deterministic() -> None:
    # GOLDEN: the key is sha256 over the NUL-joined coordinates — frozen so a
    # change to the key derivation is caught.
    expected = hashlib.sha256(
        "\x00".join(
            (
                _COORDS["exchange"],
                _COORDS["segment"],
                _COORDS["groww_symbol"],
                _COORDS["interval"],
                _COORDS["window_start"],
                _COORDS["window_end"],
            )
        ).encode("utf-8")
    ).hexdigest()
    assert archive_key(**_COORDS) == expected
    # Stable across calls.
    assert archive_key(**_COORDS) == archive_key(**_COORDS)


def _key(
    *,
    exchange: str = "NSE",
    segment: str = "CASH",
    groww_symbol: str = "NSE-NIFTY",
    interval: str = "1minute",
    window_start: str = "2024-01-01 00:00:00",
    window_end: str = "2024-01-31 00:00:00",
) -> str:
    return archive_key(
        exchange=exchange,
        segment=segment,
        groww_symbol=groww_symbol,
        interval=interval,
        window_start=window_start,
        window_end=window_end,
    )


def test_archive_key_changes_with_any_coordinate() -> None:
    base = _key()
    # Perturb each coordinate independently; every change must move the key.
    assert _key(exchange="BSE") != base
    assert _key(segment="FNO") != base
    assert _key(groww_symbol="NSE-BANKNIFTY") != base
    assert _key(interval="5minute") != base
    assert _key(window_start="2024-02-01 00:00:00") != base
    assert _key(window_end="2024-02-28 00:00:00") != base


def test_archive_path_uses_key_and_suffix(tmp_path: Path) -> None:
    cfg = _enabled(tmp_path)
    path = archive_path(cfg, **_COORDS)
    assert path.name == f"{archive_key(**_COORDS)}{ARCHIVE_SUFFIX}"
    assert path.parent == archive_dir(cfg)


def test_archive_bytes_are_deterministic_golden(tmp_path: Path) -> None:
    # GOLDEN: gzip(mtime=0) of the canonical bytes is byte-stable, so two
    # archives of the same payload produce identical files.
    cfg_a = _enabled(tmp_path / "a")
    cfg_b = _enabled(tmp_path / "b")
    path_a = archive_payload(cfg_a, _PAYLOAD, **_COORDS)
    path_b = archive_payload(cfg_b, _PAYLOAD, **_COORDS)
    assert path_a.read_bytes() == path_b.read_bytes()
    # The compressed bytes equal gzip(mtime=0) of the COMMON canonical bytes.
    assert path_a.read_bytes() == gzip.compress(canonicalize_payload(_PAYLOAD), mtime=0)


# --------------------------------------------------------------------------- #
# Archive → read-back round-trip + offline verify
# --------------------------------------------------------------------------- #
def test_archive_then_read_back_canonical_bytes(tmp_path: Path) -> None:
    cfg = _enabled(tmp_path)
    archive_payload(cfg, _PAYLOAD, **_COORDS)
    read = read_archived_canonical_bytes(cfg, **_COORDS)
    assert read == canonicalize_payload(_PAYLOAD)


def test_read_missing_returns_none(tmp_path: Path) -> None:
    assert read_archived_canonical_bytes(_enabled(tmp_path), **_COORDS) is None


def test_verify_match(tmp_path: Path) -> None:
    cfg = _enabled(tmp_path)
    record = _record()
    archive_payload(cfg, _PAYLOAD, **_COORDS)
    result = verify_archived_record(cfg, record)
    assert result.outcome is ArchiveVerifyOutcome.MATCH
    assert result.matches is True
    assert result.recorded_hash == record.payload_hash
    assert result.archived_hash == record.payload_hash
    assert result.key == record.key


def test_verify_missing(tmp_path: Path) -> None:
    cfg = _enabled(tmp_path)
    record = _record()
    result = verify_archived_record(cfg, record)
    assert result.outcome is ArchiveVerifyOutcome.MISSING
    assert result.matches is False
    assert result.archived_hash is None
    assert result.recorded_hash == record.payload_hash


def test_verify_mismatch_one_byte_tamper(tmp_path: Path) -> None:
    cfg = _enabled(tmp_path)
    record = _record()
    dest = archive_payload(cfg, _PAYLOAD, **_COORDS)
    # Tamper: rewrite the archive with a DIFFERENT (still valid gzip) canonical
    # payload so the recomputed hash diverges from the recorded one.
    tampered = {"candles": [["2024-01-01T09:15:00", 100.0, 110.0, 90.0, 999.0, 777, 42]]}
    dest.write_bytes(gzip.compress(canonicalize_payload(tampered), mtime=0))
    result = verify_archived_record(cfg, record)
    assert result.outcome is ArchiveVerifyOutcome.MISMATCH
    assert result.matches is False
    assert result.archived_hash is not None
    assert result.archived_hash != record.payload_hash


def test_verify_corrupt_archive_raises(tmp_path: Path) -> None:
    cfg = _enabled(tmp_path)
    record = _record()
    dest = archive_payload(cfg, _PAYLOAD, **_COORDS)
    dest.write_bytes(b"not-a-gzip-stream")  # corrupt gzip member
    with pytest.raises(OSError):
        verify_archived_record(cfg, record)


def test_archive_idempotent_overwrite(tmp_path: Path) -> None:
    cfg = _enabled(tmp_path)
    first = archive_payload(cfg, _PAYLOAD, **_COORDS)
    second = archive_payload(cfg, _PAYLOAD, **_COORDS)
    assert first == second
    assert list(archive_dir(cfg).iterdir()) == [first]


# --------------------------------------------------------------------------- #
# Hook: disabled no-op, enabled write, best-effort swallow, factory
# --------------------------------------------------------------------------- #
def test_disabled_hook_is_noop(tmp_path: Path) -> None:
    cfg = RawPayloadArchiveConfig(root=tmp_path, enabled=False)
    hook = make_archive_hook(cfg, exchange="NSE", segment="CASH", groww_symbol="NSE-NIFTY")
    hook(_PAYLOAD, "1minute", _COORDS["window_start"], _COORDS["window_end"])
    # No directory + no file written when disabled.
    assert not archive_dir(cfg).exists()


def test_enabled_hook_writes(tmp_path: Path) -> None:
    cfg = _enabled(tmp_path)
    hook = make_archive_hook(cfg, exchange="NSE", segment="CASH", groww_symbol="NSE-NIFTY")
    hook(_PAYLOAD, "1minute", _COORDS["window_start"], _COORDS["window_end"])
    assert read_archived_canonical_bytes(cfg, **_COORDS) == canonicalize_payload(_PAYLOAD)


def test_hook_swallows_malformed_payload(tmp_path: Path) -> None:
    cfg = _enabled(tmp_path)
    hook = make_archive_hook(cfg, exchange="NSE", segment="CASH", groww_symbol="NSE-NIFTY")
    # A non-mapping payload makes canonicalize_payload raise RawIntegrityError;
    # the best-effort hook must swallow it (never raise).
    hook("not-a-mapping", "1minute", _COORDS["window_start"], _COORDS["window_end"])  # type: ignore[arg-type]
    assert not archive_dir(cfg).exists() or list(archive_dir(cfg).iterdir()) == []


def test_hook_swallows_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _enabled(tmp_path)

    def _boom(*_args: object, **_kwargs: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr("brutex.data.raw_payload_archive.archive_payload", _boom)
    hook = make_archive_hook(cfg, exchange="NSE", segment="CASH", groww_symbol="NSE-NIFTY")
    # Must not raise.
    hook(_PAYLOAD, "1minute", _COORDS["window_start"], _COORDS["window_end"])


def test_malformed_payload_propagates_from_archive_payload(tmp_path: Path) -> None:
    # The pure archive_payload (not the hook) DOES propagate the error.
    with pytest.raises(RawIntegrityError):
        archive_payload(_enabled(tmp_path), "bad", **_COORDS)  # type: ignore[arg-type]


def test_factory_attributes_per_symbol(tmp_path: Path) -> None:
    cfg = _enabled(tmp_path)
    factory = make_archive_hook_factory(cfg)
    hook = factory("NSE", "FNO", "NSE-NIFTY-02Jan25-28500-PE")
    hook(_PAYLOAD, "1minute", _COORDS["window_start"], _COORDS["window_end"])
    read = read_archived_canonical_bytes(
        cfg,
        exchange="NSE",
        segment="FNO",
        groww_symbol="NSE-NIFTY-02Jan25-28500-PE",
        interval="1minute",
        window_start=_COORDS["window_start"],
        window_end=_COORDS["window_end"],
    )
    assert read == canonicalize_payload(_PAYLOAD)


def test_factory_disabled_writes_nothing(tmp_path: Path) -> None:
    cfg = RawPayloadArchiveConfig(root=tmp_path, enabled=False)
    factory = make_archive_hook_factory(cfg)
    hook = factory("NSE", "CASH", "NSE-NIFTY")
    hook(_PAYLOAD, "1minute", _COORDS["window_start"], _COORDS["window_end"])
    assert not archive_dir(cfg).exists()


# --------------------------------------------------------------------------- #
# Property: archive→read-back→hash matches; 1-byte tamper detected
# --------------------------------------------------------------------------- #
_candle = st.lists(
    st.one_of(
        st.text(min_size=0, max_size=8),
        st.integers(min_value=-1000, max_value=1000),
        st.floats(allow_nan=False, allow_infinity=False, width=32),
    ),
    min_size=1,
    max_size=7,
)


@settings(deadline=None)
@given(candles=st.lists(_candle, min_size=0, max_size=6))
def test_property_roundtrip_hash_matches(tmp_path_factory: Any, candles: Any) -> None:
    root = tmp_path_factory.mktemp("prop")
    cfg = _enabled(root)
    payload = {"candles": candles}
    record = build_record(payload, **_COORDS)
    archive_payload(cfg, payload, **_COORDS)
    result = verify_archived_record(cfg, record)
    # The archived bytes recompute to EXACTLY the recorded hash.
    assert result.outcome is ArchiveVerifyOutcome.MATCH
    assert result.archived_hash == hash_payload(payload)


@settings(deadline=None)
@given(data=st.binary(min_size=1, max_size=64))
def test_property_corrupt_bytes_never_silent_pass(tmp_path_factory: Any, data: Any) -> None:
    # A non-gzip / corrupt archive must EITHER raise on read OR (if it happens to
    # decompress) NOT silently match the recorded hash of the real payload.
    root = tmp_path_factory.mktemp("corrupt")
    cfg = _enabled(root)
    record = _record()
    dest = archive_payload(cfg, _PAYLOAD, **_COORDS)
    dest.write_bytes(data)
    try:
        result = verify_archived_record(cfg, record)
    except OSError:
        return  # corrupt gzip → inconclusive (acceptable: not a silent pass)
    # If it decoded, it must not equal the real payload's hash by accident.
    assert result.outcome is not ArchiveVerifyOutcome.MISSING
    if result.outcome is ArchiveVerifyOutcome.MATCH:
        # Only acceptable if the corrupt bytes happened to be a valid gzip of the
        # exact canonical payload — astronomically unlikely but logically allowed.
        assert read_archived_canonical_bytes(cfg, **_COORDS) == canonicalize_payload(_PAYLOAD)
