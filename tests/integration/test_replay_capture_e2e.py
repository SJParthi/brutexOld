"""Fake-vendor END-TO-END replay-oracle pulls (DEC-AUDIT-LAKE-REPLAY-ORACLE-001 §3).

Drives the REAL production spine — :class:`~tests.fakes.fake_groww_sdk.FakeGrowwSDK`
through the real :func:`~brutex.adapters.groww.live.build_live_pool` →
:class:`~brutex.adapters.groww.client.GrowwClient` → parser →
:class:`~brutex.adapters.groww.adapter.GrowwMarketAdapter` →
:func:`~brutex.adapters.groww.run_ingest.run_groww_spot_ingest` — fully
OFFLINE (hermetic token hooks, zero injected latency, no ``growwapi``, no
network), with the REAL hash-chained :class:`~brutex.orchestrator.audit.AuditWriter`
and a CAPTURE-mode :class:`~brutex.data.audit_capture.AuditCaptureSink` wired
exactly like a forensic production pull:

* **(a) P-20:** the audit log ALONE rebuilds the lake byte-for-byte
  (:func:`~brutex.orchestrator.lake_replay.replay_lake_from_audit` +
  :func:`~brutex.orchestrator.lake_replay.assert_lake_byte_equal`), with the
  XOR-one-byte mutation tooth proving the oracle bites.
* **(b) L3:** the live :class:`~brutex.data.lmdb_membership.LmdbMembership`
  dedup set equals the chain's replayed ``action="add"`` subjects via the
  UNMODIFIED :func:`brutex.orchestrator.replay.assert_dedup_consistent`
  (#241) — with divergence teeth both ways (an extra key physically injected
  into the live LMDB → ``extra_in_live``; a dropped subject →
  ``missing_from_live``) and the idempotence tooth: a RE-PULL of the same
  range (whose fake-vendor bar VALUES differ, but whose timestamps — the
  dedup identity — repeat) admits ZERO new keys, leaves the LMDB and the
  lake bytes unchanged, keeps the oracle silent, AND the P-20 replay of the
  GROWN log stays byte-equal (the two oracles cross-checked in one run).

The hermetic pool wiring mirrors ``tests/bench/pull_harness/runner.py``
(fake ``token_factory`` / ``totp_minter`` + explicit ``token_cache_*`` hooks
so the AWS Parameter Store backend is never resolved), inlined here so the
gated integration tree never imports from the coverage-excluded bench tree.

Bounded (2 symbols × 1 window × ≤120 bars each), deterministic
(:class:`~brutex.orchestrator.parallel.SerialRunParallel`), offline.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from brutex.adapters.groww.adapter import GrowwMarketAdapter
from brutex.adapters.groww.auth import GrowwCredentials
from brutex.adapters.groww.constants import INSTRUMENT_CSV_COLUMNS
from brutex.adapters.groww.instruments import load_index_from_csv
from brutex.adapters.groww.live import build_live_pool
from brutex.adapters.groww.run_ingest import run_groww_spot_ingest
from brutex.data.audit_capture import (
    DEDUP_ADD_ACTION,
    AuditCaptureSink,
    CaptureMode,
    DedupJournal,
)
from brutex.data.lmdb_membership import LmdbMembership
from brutex.data.option_dedup import OptionDedupIndex
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.models import Exchange, Instrument
from brutex.orchestrator.audit import AuditError, AuditWriter, read_entries, verify_chain
from brutex.orchestrator.lake_replay import (
    ReplayMismatchError,
    assert_lake_byte_equal,
    check_capture_anchors,
    replay_lake_from_audit,
)
from brutex.orchestrator.parallel import SerialRunParallel
from brutex.orchestrator.replay import assert_dedup_consistent

from tests.fakes.fake_groww_sdk import FakeGrowwSDK, make_fake_sdk_factory

_CREDS = GrowwCredentials(api_key="e2e_key", totp_secret="e2e_secret")

#: Two trading days (Mon 2021-01-04 → Wed 2021-01-06 exclusive) — a single
#: 30-day-tier 1-minute vendor window per symbol; the fake emits its bars
#: from the window's first session open (09:15 IST), all in-session.
_START = date(2021, 1, 4)
_END = date(2021, 1, 6)

#: Bars the fake vendor returns per window — all inside the 09:15-15:30
#: session of the window's first day, so the session filter keeps every one.
_BARS_PER_WINDOW = 90

_SYMBOLS = ["SYNA", "SYNB"]


def _instrument_csv(symbols: list[str]) -> str:
    """An ``instrument.csv`` body of synthetic NSE cash-equity rows.

    Each row resolves through the REAL master path (``by_exchange_symbol`` +
    ``groww_symbol_by_pair``) so the adapter sends the real ``groww_symbol``.
    """
    header = ",".join(INSTRUMENT_CSV_COLUMNS)
    lines = [header]
    for i, symbol in enumerate(symbols):
        cells = dict.fromkeys(INSTRUMENT_CSV_COLUMNS, "")
        cells.update(
            exchange="NSE",
            exchange_token=str(2000 + i),
            trading_symbol=symbol,
            groww_symbol=f"NSE-{symbol}",
            name=f"Synthetic {symbol}",
            instrument_type="EQ",
            segment="CASH",
            series="EQ",
            lot_size="1",
            tick_size="0.05",
        )
        lines.append(",".join(cells[c] for c in INSTRUMENT_CSV_COLUMNS))
    return "\n".join(lines) + "\n"


def _build_adapter(symbols: list[str]) -> tuple[GrowwMarketAdapter, list[Instrument]]:
    """The REAL pool/client/parser path over a :class:`FakeGrowwSDK` — offline.

    Hermetic token hooks (the ``pull_harness/runner.py`` wiring): with BOTH
    cache hooks explicitly injected, ``_resolve_token_cache_hooks`` takes the
    explicit-hook path and never resolves the AWS Parameter Store backend;
    the fake ``token_factory`` mints the token, the fake ``totp_minter``
    never touches ``pyotp`` secrets. Zero injected latency (deterministic +
    fast); no 429/timeout bursts (a clean pull — the retry ladders are
    exercised by their own suites).
    """
    index = load_index_from_csv(_instrument_csv(symbols))
    template = FakeGrowwSDK(
        seed=7,
        min_latency_s=0.0,
        max_latency_s=0.0,
        candles_per_window=_BARS_PER_WINDOW,
    )
    pool = build_live_pool(
        _CREDS,
        size=1,
        token_factory=lambda _k, _t: "E2E-TOKEN",
        totp_minter=lambda _s: "000000",
        sdk_factory=make_fake_sdk_factory(template),  # type: ignore[arg-type]
        token_cache_load=lambda **_kw: None,
        token_cache_store=lambda *_a, **_kw: True,
    )
    instruments = [index.by_exchange_symbol[(Exchange.NSE, s)] for s in symbols]
    return GrowwMarketAdapter(client=pool, index=index), instruments


def _pull(
    adapter: GrowwMarketAdapter,
    instruments: list[Instrument],
    capture: AuditCaptureSink,
    audit_path: Path,
) -> None:
    """One END-TO-END pull through the real spine with audit + capture wired."""
    with AuditWriter.open(audit_path, resume=audit_path.exists()) as audit:
        report = run_groww_spot_ingest(
            adapter,
            capture,
            instruments=instruments,
            intervals=["1minute"],
            start=_START,
            end=_END,
            audit=audit,
            capture=capture,
            run_parallel=SerialRunParallel(),
        )
    assert report.failed_pairs == 0
    assert report.bars_appended == len(instruments) * _BARS_PER_WINDOW


def _bars_tree(lake_root: Path) -> dict[str, bytes]:
    """Map each ``bars/**/*.parquet`` file's lake-relative path → its bytes."""
    root = lake_root / "bars"
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*.parquet"))}


class TestP20ReplayOverFakeVendorPull:
    """(a) P-20 replay byte-equality over a REAL end-to-end fake-vendor pull."""

    def test_replay_is_byte_equal_and_mutation_tooth_bites(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        replayed = tmp_path / "replayed"
        audit_path = tmp_path / "audit" / "spot-ingest.jsonl"
        audit_path.parent.mkdir(parents=True)
        adapter, instruments = _build_adapter(_SYMBOLS)
        capture = AuditCaptureSink(
            ParquetBarSink(writer=ParquetWriter(lake_root=lake)),
            lake_root=lake,
            mode=CaptureMode.CAPTURE,
        )
        _pull(adapter, instruments, capture, audit_path)

        # RUN_START + 2 × (PAIR_DONE + one drained capture event) + RUN_COMPLETE.
        assert verify_chain(audit_path) == 6

        report = replay_lake_from_audit(audit_path, replay_root=replayed)
        assert report.appends_replayed == 2
        assert report.anchor_matches == 2
        assert report.partitions == frozenset(
            {
                "bars/NSE/CASH/SYNA/1minute/2021/01.parquet",
                "bars/NSE/CASH/SYNB/1minute/2021/01.parquet",
            }
        )
        # THE P-20 oracle: full byte equality, strict.
        assert_lake_byte_equal(original_root=lake, replay_root=replayed, report=report, strict=True)
        # Independent recompute: the trees really are byte-identical, and the
        # audited raw_hash anchors match the LIVE lake bytes.
        original_tree = _bars_tree(lake)
        assert original_tree
        assert _bars_tree(replayed) == original_tree
        anchors = check_capture_anchors(audit_path, lake_root=lake)
        assert anchors.anchor_mismatches == ()
        assert anchors.anchor_matches == 2

        # TOOTH: XOR one byte of a live partition → loud ReplayMismatchError.
        rel = min(report.partitions)
        target = lake / rel
        mutated = bytearray(target.read_bytes())
        mutated[21] ^= 0xFF
        target.write_bytes(bytes(mutated))
        with pytest.raises(ReplayMismatchError) as excinfo:
            assert_lake_byte_equal(
                original_root=lake, replay_root=replayed, report=report, strict=True
            )
        message = str(excinfo.value)
        assert rel in message
        assert "first divergent byte offset 21" in message
        assert "mutated AFTER the audited write" in message


class TestL3LiveLmdbOracleOverFakeVendorPull:
    """(b) L3: live LMDB set vs the chain's replayed ``add`` subjects — with teeth."""

    def test_live_lmdb_matches_replayed_set_teeth_and_idempotent_repull(
        self, tmp_path: Path
    ) -> None:
        lake = tmp_path / "lake"
        audit_path = tmp_path / "audit" / "spot-ingest.jsonl"
        audit_path.parent.mkdir(parents=True)
        adapter, instruments = _build_adapter(_SYMBOLS)
        expected_keys = len(_SYMBOLS) * _BARS_PER_WINDOW
        with LmdbMembership.open(tmp_path / "dedup-live.lmdb", map_size_mb=64) as membership:
            journal = DedupJournal(OptionDedupIndex(exact=membership))
            capture = AuditCaptureSink(
                ParquetBarSink(writer=ParquetWriter(lake_root=lake)),
                lake_root=lake,
                mode=CaptureMode.CAPTURE,
                journal=journal,
            )
            _pull(adapter, instruments, capture, audit_path)
            assert verify_chain(audit_path) > 0
            live = frozenset(key.hex() for key in membership.iter_keys())
            assert len(live) == expected_keys

            # The UNMODIFIED #241 oracle: silence iff live == replayed.
            assert_dedup_consistent(audit_path=audit_path, live_subjects=live)

            # ── Idempotence tooth: RE-PULL the same range through the SAME
            # capture sink + journal. The fake vendor's bar VALUES differ on
            # the second pass (its RNG advances with the physical call index)
            # but the TIMESTAMPS — the dedup identity — repeat, so keep-first
            # stores nothing, the journal admits nothing, and the oracle
            # stays silent.
            tree_first = _bars_tree(lake)
            _pull(adapter, instruments, capture, audit_path)
            assert verify_chain(audit_path) > 0
            live_after = frozenset(key.hex() for key in membership.iter_keys())
            assert live_after == live  # LMDB unchanged
            add_rows = [e for e in read_entries(audit_path) if e.action == DEDUP_ADD_ACTION]
            assert len(add_rows) == expected_keys  # zero NEW add rows
            assert _bars_tree(lake) == tree_first  # lake bytes unchanged
            assert_dedup_consistent(audit_path=audit_path, live_subjects=live_after)

            # P-20 cross-check: the GROWN log (pull + conflicting re-pull)
            # still replays byte-equal through the real keep-first merge.
            replayed = tmp_path / "replayed"
            report = replay_lake_from_audit(audit_path, replay_root=replayed)
            assert report.appends_replayed == 4
            assert_lake_byte_equal(
                original_root=lake, replay_root=replayed, report=report, strict=True
            )

            # ── Divergence tooth 1: drop one dedup key from a copied
            # snapshot → the oracle fails naming missing_from_live.
            with pytest.raises(AuditError, match="missing_from_live=1"):
                assert_dedup_consistent(
                    audit_path=audit_path, live_subjects=live_after - {min(live_after)}
                )

            # ── Divergence tooth 2: physically inject one extra key into the
            # REAL live LMDB (a write that bypassed the audit) → extra_in_live.
            membership.add(b"\xab" * 32)
            dirty = frozenset(key.hex() for key in membership.iter_keys())
            assert len(dirty) == expected_keys + 1
            with pytest.raises(AuditError, match="extra_in_live=1"):
                assert_dedup_consistent(audit_path=audit_path, live_subjects=dirty)
