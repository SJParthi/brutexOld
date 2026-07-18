"""FROZEN pre-conversion oracle golden — the L14 lane's byte/value-parity net.

DEC-RUST-RUNID-STORES-001. The fixture
(``tests/golden/fixtures/runid_stores_oracle_v1.json``) was captured from the
LAST all-Python implementation of the four converted surfaces — BEFORE the
``brutex_identity`` kernel existed — and is never regenerated silently
(``BRUTEX_REGEN_RUNID_ORACLE`` guard). This test drives the CURRENT
(Rust-backed) implementations over the SAME input corpora (imported from the
generator — ONE source, CLAUDE.md §11 COMMON) and asserts every captured
output byte-for-byte, proving the conversion changed no observable value on:

* ``canonicalise_params`` — the canonical-JSON byte law (CPython ``repr``
  floats, ``json.dumps`` sorted/compact/``ensure_ascii`` escaping);
* ``combo_hash`` — the 9-field BLAKE3 run identity (Golden Rule #4);
* ``data_snapshot_hash`` — the streamed ``bxsnap2`` digest;
* the results-store append/VIX-merge/conflict law + ``top_k`` ordering;
* the window-ledger merge-on-save law + the legacy-column derivations + the
  row FIXED POINT through the canonical Parquet codec (the #1917 G10
  option-(iii) correctness law — the BYTES re-baselined, the ROWS proven).

Run per push in CI (the kernel-drift net the import-time symbol handshake
deliberately does not carry — see ``brutex_identity/__init__.py``).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.data.window_ledger_store import (
    _derive_empty_streak,
    _derive_in_pull_fetches,
    _read_ledger_file_rows,
    merge_ledger_rows,
    render_ledger_parquet,
)
from brutex.engine.results_store import (
    ResultRow,
    ResultsStore,
    ResultsStoreError,
    ResultsStoreGateConflictError,
    VixTrioConflictError,
)
from brutex.engine.snapshot_hash import data_snapshot_hash
from brutex.strategies.combo import canonicalise_params, combo_hash

from tests.golden.generate_runid_stores_fixtures import (
    FIXTURE_PATH,
    append_inputs,
    canonical_inputs,
    combo_inputs,
    ledger_merge_inputs,
    ledger_roundtrip_inputs,
    snapshot_inputs,
    topk_inputs,
)

# pyright: reportPrivateUsage=none


@pytest.fixture(scope="module")
def oracle() -> dict[str, Any]:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    return payload  # type: ignore[no-any-return]


def _revive_row_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    out = dict(kwargs)
    for name in ("avg_entry_price", "avg_exit_price"):
        out[name] = Decimal(out[name])
    return out


class TestCanonicalParams:
    def test_every_captured_case_is_byte_identical(self, oracle: dict[str, Any]) -> None:
        cases = oracle["canonical_params"]
        inputs = canonical_inputs()
        assert [c["name"] for c in cases] == [name for name, _ in inputs]
        for (name, params), case in zip(inputs, cases, strict=True):
            assert canonicalise_params(params) == case["expected_json"], name


class TestComboHash:
    def test_every_captured_digest_is_identical(self, oracle: dict[str, Any]) -> None:
        cases = oracle["combo_hash"]
        inputs = combo_inputs()
        assert [c["name"] for c in cases] == [name for name, _ in inputs]
        for (name, kwargs), case in zip(inputs, cases, strict=True):
            assert combo_hash(**kwargs) == case["expected_hex"], name


class TestSnapshotHash:
    def test_every_captured_digest_is_identical(self, oracle: dict[str, Any]) -> None:
        cases = oracle["snapshot_hash"]
        inputs = snapshot_inputs()
        assert [c["name"] for c in cases] == [name for name, _ in inputs]
        for (name, kwargs), case in zip(inputs, cases, strict=True):
            assert data_snapshot_hash(**kwargs) == case["expected_hex"], name


class TestAppendLaw:
    def test_every_captured_outcome_matches(self, oracle: dict[str, Any]) -> None:
        cases = oracle["append_law"]
        inputs = append_inputs()
        assert [c["name"] for c in cases] == [name for name, _, _ in inputs]
        for (name, existing_kw, incoming_kw), case in zip(inputs, cases, strict=True):
            store = ResultsStore(path=Path("/dev/null"))
            if existing_kw is not None:
                store.append(ResultRow(**_revive_row_kwargs(existing_kw)))
            expected = case["outcome"]
            try:
                changed = store.append(ResultRow(**_revive_row_kwargs(incoming_kw)))
            except VixTrioConflictError:
                got: dict[str, Any] = {"kind": "vix_conflict"}
            except ResultsStoreGateConflictError:
                got = {"kind": "gate_conflict"}
            except ResultsStoreError:
                got = {"kind": "plain_conflict"}
            else:
                stored = store.get(str(incoming_kw["run_id"]))
                assert stored is not None
                got = {
                    "kind": "ok",
                    "changed": changed,
                    "stored_vix": [stored.vix_mean, stored.vix_min, stored.vix_max],
                }
            assert got == expected, name


class TestTopK:
    def test_every_captured_ordering_matches(self, oracle: dict[str, Any]) -> None:
        rows, queries = topk_inputs()
        store = ResultsStore(path=Path("/dev/null"))
        for kwargs in rows:
            store.append(ResultRow(**_revive_row_kwargs(kwargs)))
        cases = oracle["top_k"]
        assert len(cases) == len(queries)
        for query, case in zip(queries, cases, strict=True):
            ranked = store.top_k(
                int(query["k"]), by=str(query["by"]), descending=bool(query["descending"])
            )
            assert [r.run_id for r in ranked] == case["expected_run_ids"], query


class TestLedgerMerge:
    def test_every_captured_merge_matches(self, oracle: dict[str, Any]) -> None:
        cases = oracle["ledger_merge"]
        inputs = ledger_merge_inputs()
        assert [c["name"] for c in cases] == [name for name, _ in inputs]
        for (name, kwargs), case in zip(inputs, cases, strict=True):
            merged = merge_ledger_rows(
                [tuple(r) for r in kwargs["memory_rows"]],
                [tuple(r) for r in kwargs["disk_rows"]],
                pruned_keys=frozenset(tuple(k) for k in kwargs.get("pruned_keys", [])),
                reopened_keys=frozenset(tuple(k) for k in kwargs.get("reopened_keys", [])),
                degraded_keys=frozenset(tuple(k) for k in kwargs.get("degraded_keys", [])),
            )
            assert merged == [tuple(r) for r in case["expected"]], name


class TestLegacyDerivations:
    def test_every_captured_derivation_matches(self, oracle: dict[str, Any]) -> None:
        cases = oracle["ledger_legacy_derivations"]
        assert len(cases) == 30  # 6 statuses × 5 attempt counts — anti-vacuous
        for case in cases:
            status, attempts = str(case["status"]), int(case["attempts"])
            assert _derive_empty_streak(status, attempts) == case["empty_streak"], case
            assert _derive_in_pull_fetches(status) == case["in_pull_fetches"], case
            assert case["bars_net_new"] is None  # honest-unknown law, frozen
            assert case["retry_split"] is None


class TestLedgerRowFixedPoints:
    def test_every_captured_corpus_round_trips(
        self, oracle: dict[str, Any], tmp_path: Path
    ) -> None:
        cases = oracle["ledger_row_fixed_points"]
        inputs = ledger_roundtrip_inputs()
        assert [c["name"] for c in cases] == [name for name, _ in inputs]
        for (name, rows), case in zip(inputs, cases, strict=True):
            # The CAPTURED rows equal the input rows (the fixture froze the
            # pre-conversion pyarrow round-trip's output — the fixed point is
            # encoder-independent, so the canonical codec must reproduce it).
            assert [list(r) for r in rows] == case["rows"], name
            path = tmp_path / f"{name}.parquet"
            path.write_bytes(render_ledger_parquet(list(rows)))
            assert _read_ledger_file_rows(path) == list(rows), name
