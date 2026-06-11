"""Unit tests for the per-tuple completion sentinel (`brutex.engine.tuple_complete`).

100% line + branch coverage of the redo-authority artifact
(DEC-MARK-MANY-SENTINEL-001): every :class:`TupleCompletionSentinel`
validation arm, the path helper's suffix discipline, deterministic
serialization, the atomic write/read round-trip, and every
:func:`read_completion_sentinel` error arm. The sweep-level semantics (the
short-circuit / bypass / self-heal decision table) are covered end-to-end in
``tests/unit/test_engine_sweep_run.py::TestCompletionSentinel``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from brutex.engine.tuple_complete import (
    COMPLETION_SCHEMA_VERSION,
    COMPLETION_SUFFIX,
    TupleCompletionError,
    TupleCompletionSentinel,
    completion_sentinel_path,
    read_completion_sentinel,
    render_completion_sentinel,
    write_completion_sentinel,
)

_DIGEST = "ab12cd34ef56ab12"
_HASH = "0f" * 32


def _sentinel(**overrides: object) -> TupleCompletionSentinel:
    base: dict[str, object] = {
        "tuple_digest": _DIGEST,
        "store_content_hash": _HASH,
        "sidecar_written": True,
        "n_marked": 5,
    }
    base.update(overrides)
    return TupleCompletionSentinel(**base)  # type: ignore[arg-type]


class TestValidation:
    def test_valid_constructs(self) -> None:
        sentinel = _sentinel()
        assert sentinel.tuple_digest == _DIGEST
        assert sentinel.store_content_hash == _HASH
        assert sentinel.sidecar_written is True
        assert sentinel.n_marked == 5

    def test_negative_n_marked_raises(self) -> None:
        with pytest.raises(TupleCompletionError, match="n_marked must be >= 0"):
            _sentinel(n_marked=-1)

    def test_non_bool_sidecar_written_raises(self) -> None:
        with pytest.raises(TupleCompletionError, match="sidecar_written must be a bool"):
            _sentinel(sidecar_written=1)

    @pytest.mark.parametrize(
        "digest",
        ["short", "A" * 16, "zz12cd34ef56ab12"],
        ids=["wrong-length", "uppercase", "non-hex"],
    )
    def test_bad_tuple_digest_raises(self, digest: str) -> None:
        with pytest.raises(TupleCompletionError, match="tuple_digest"):
            _sentinel(tuple_digest=digest)

    def test_bad_store_hash_raises(self) -> None:
        with pytest.raises(TupleCompletionError, match="store_content_hash must be 64 hex"):
            _sentinel(store_content_hash="abc")


class TestPathHelper:
    def test_suffix_appends_to_the_full_store_name(self, tmp_path: Path) -> None:
        store = tmp_path / "NIFTY" / "1minute" / "bull_k3_ab12cd34ef56ab12.parquet"
        sentinel = completion_sentinel_path(store)
        assert sentinel.parent == store.parent
        assert sentinel.name == store.name + COMPLETION_SUFFIX
        assert sentinel.name.endswith(".parquet.complete.json")


class TestSerialization:
    def test_render_is_deterministic(self) -> None:
        assert render_completion_sentinel(_sentinel()) == render_completion_sentinel(_sentinel())

    def test_render_exact_bytes(self) -> None:
        # The byte-determinism pin: sorted keys, compact separators.
        rendered = render_completion_sentinel(_sentinel())
        assert rendered == (
            b'{"n_marked":5,"schema_version":1,'
            b'"sidecar_written":true,'
            b'"store_content_hash":"' + _HASH.encode() + b'",'
            b'"tuple_digest":"' + _DIGEST.encode() + b'"}'
        )

    def test_write_read_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "r.parquet.complete.json"
        sentinel = _sentinel(sidecar_written=False, n_marked=0)
        assert write_completion_sentinel(path, sentinel) == path  # mkdir -p'd
        assert read_completion_sentinel(path) == sentinel


class TestReadErrorArms:
    def _write(self, tmp_path: Path, payload: object) -> Path:
        path = tmp_path / "s.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(TupleCompletionError, match="not found"):
            read_completion_sentinel(tmp_path / "nope.json")

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "s.json"
        path.write_text("{corrupt", encoding="utf-8")
        with pytest.raises(TupleCompletionError, match="not valid JSON"):
            read_completion_sentinel(path)

    def test_non_object_payload_raises(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, [1, 2])
        with pytest.raises(TupleCompletionError, match="must be a JSON object"):
            read_completion_sentinel(path)

    def test_unsupported_schema_version_raises(self, tmp_path: Path) -> None:
        payload = json.loads(render_completion_sentinel(_sentinel()))
        payload["schema_version"] = COMPLETION_SCHEMA_VERSION + 1
        with pytest.raises(TupleCompletionError, match="unsupported completion-sentinel"):
            read_completion_sentinel(self._write(tmp_path, payload))

    def test_missing_key_raises(self, tmp_path: Path) -> None:
        payload = json.loads(render_completion_sentinel(_sentinel()))
        del payload["n_marked"]
        with pytest.raises(TupleCompletionError, match="missing keys.*n_marked"):
            read_completion_sentinel(self._write(tmp_path, payload))

    def test_non_bool_sidecar_written_raises(self, tmp_path: Path) -> None:
        payload = json.loads(render_completion_sentinel(_sentinel()))
        payload["sidecar_written"] = "yes"
        with pytest.raises(TupleCompletionError, match="non-boolean sidecar_written"):
            read_completion_sentinel(self._write(tmp_path, payload))

    def test_malformed_field_raises(self, tmp_path: Path) -> None:
        payload = json.loads(render_completion_sentinel(_sentinel()))
        payload["n_marked"] = "not-a-number"
        with pytest.raises(TupleCompletionError, match="malformed field"):
            read_completion_sentinel(self._write(tmp_path, payload))

    def test_invalid_field_value_raises_typed(self, tmp_path: Path) -> None:
        # A field that parses but violates an invariant surfaces the
        # constructor's own typed error (already a TupleCompletionError).
        payload = json.loads(render_completion_sentinel(_sentinel()))
        payload["tuple_digest"] = "0" * 15
        with pytest.raises(TupleCompletionError, match="tuple_digest must be 16 hex"):
            read_completion_sentinel(self._write(tmp_path, payload))
