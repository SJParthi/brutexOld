"""Deterministic generator for the one-click artifact fixture corpus (DEC-OPERATOR-PAGE-REAL-001).

Regenerate with::

    python tests/golden/generate_oneclick_fixture.py

Writes ``tests/fixtures/oneclick/sweep_runs/**`` — CONTRACT-shaped fixtures
for the P4 display build (sweep-parallel-integration-contracts; the tree
migrated ``runs/`` → ``sweep_runs/`` under gap-register Ruling 4 /
DEC-ONECLICK-SWEEP-RUNS-REBIND-001 — the fixture root is a pseudo LAKE root,
so runs live at the same ``sweep_runs/`` sibling the production writers
use): the P2 run manifest,
the P3 top-100 leaderboard, the P2 flight-recorder events JSONL (hash-chained
via the SAME :func:`brutex.dashboard.oneclick_artifacts.chain_hash` seam the
verifier uses, so the golden proves generator↔verifier SELF-consistency ONLY —
NOT P2 compatibility), and the INTERIM fixture-only trades page layout
(``trades/rank-%04d.json``), plus the empty/short/corrupt/hostile twins.

TOP100 SHAPE LAW (DEC-ONECLICK-TOP100-OBJECT-ROOT-001): every ``top100.json``
twin is the SHIPPED engine writer's OBJECT root ``{schema_version, run_id,
preset, source, baselines, entries: [...]}`` with the writer's ENGINE metric
names (``sharpe_ratio`` / ``max_drawdown_paisa`` / ``deflated_sharpe`` /
``total_trades`` — the display aliases are added by the reader's ONE
``TOP100_METRIC_KEY_MAP`` boundary) and NO ``cpcv_pass_pct`` (the writer
emits none — the display renders the honest "—"). The pre-#1621 bare-array
form was a hand-written fixture-only dialect the real writer never produced —
the audit lesson this generator now encodes: fixtures match the WRITER's
shape, not the reader's wishes. The hostile-round F1/F2 fold finished the
per-key alignment to ``top100_artifact._entry_payload``: ``arm`` is the
writer's DICT ``{sl_paisa, tp_paisa}`` (the reader bridges it to the display
string — a string-armed fixture had MASKED the "[object Object]" render bug),
``mask_hex`` is the writer's 32-hex NO-``0x`` form, ``regime`` carries the
writer's key set (``by_year``/``by_trend``/``one_regime_wonder``/
``regime_wonder_axis``/``entry_hour_hist``/``event_days``), ``vix_band``
carries the writer's full key set (``lo``/``hi``/``stamped``/``missing``/
``win``/``loss``), and the always-emitted ``instrument``/``family_members``/
``reasons``/``margin_roi`` keys are present (``reasons``/``margin_roi`` are
the writer's honest ``null`` — providers unwired, matching ``source``). The
ONLY writer keys the entries still OMIT — deliberately, documented here —
are the #1275 additive ``run_id``/``equity_ref``, so the pre-#1275-artifact
trades degrade stays pinned; the edge-class rows (tie / zero-trades /
negative-PF / huge-DD / null-vix [a JSON ``null`` band the writer never
emits] / null-arm / hostile strings) remain display-robustness twins, not
writer byte-dumps.

Every number is deterministic pure arithmetic (no RNG, no clock) so a
regeneration is byte-identical; the consuming golden test pins a SHA-256
manifest over the corpus (tests/golden/fixtures/README.md fixture law).

The main-run manifest funnel is HAND-DERIVED so R44 check #7 reconciles to
the digit: tested 33,800 − gates 25,800 = 8,000 − rank 6,800 = 1,200 −
refine 800 = 400 − cascade 300 = 100, and enumerated 36,050 == tested
33,800 + skipped 2,250.

The hostile twin (``r-hostile01``) seeds ``<script>``/``onerror=``/``<svg
onload=…>`` strings in ``conditions[]``, event ``kind`` and ``payload`` — the
positive-escape XSS fixture (F-M2); the display layer must render them as
DATA only.
"""

# The generator deliberately shares the adapter's private chain-genesis
# constant (ONE algorithm definition).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from brutex.dashboard import oneclick_artifacts
from brutex.dashboard.oneclick_artifacts import (
    TOP100_SCHEMA_VERSION,
    canonical_record_bytes,
    chain_hash,
)
from brutex.engine.resilience_streaks import fold_resilience, resilience_payload
from brutex.ranking.presets import DEFAULT_PRESET

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "oneclick"

RUN_ID = "r-20260707-fixt01"
EMPTY_RUN_ID = "r-empty01"
SHORT_RUN_ID = "r-short01"
CORRUPT_RUN_ID = "r-corrupt01"
HOSTILE_RUN_ID = "r-hostile01"

SHORT_ROW_COUNT = 17
TRADE_ROW_COUNT = 412
TIE_RANKS = (41, 42)  # identical sharpe — the F-m2 rank-ascending tie-break golden pair
NULL_ARM_RANK = 96  # writer-truth None legs — the reader renders "SL — · TP —"
ZERO_TRADES_RANK = 97
NEGATIVE_PF_RANK = 98  # display-robustness edge: impossible in reality, must not crash a renderer
HUGE_DD_RANK = 99
NULL_VIX_RANK = 100

TFS = ("5m", "15m", "1h")
DIRECTIONS = ("long", "short")
EXIT_REASONS = ("TP", "SL", "signal-flip", "forced-15:20")

#: Hostile strings for the positive-escape XSS fixture (F-M2).
HOSTILE_STRINGS = (
    "<script>alert(1)</script>",
    "<svg onload=alert(1)>",
    'x" onerror="alert(1)',
)

#: Hand-derived funnel arithmetic (see module docstring).
FUNNEL_STAGES = (
    ("tested", 33_800),
    ("gates", 8_000),
    ("rank", 1_200),
    ("refine", 400),
    ("cascade", 100),
)
DOOR_KILLS = (
    ("gates", 25_800),
    ("rank", 6_800),
    ("refine", 800),
    ("cascade", 300),
)
ENUMERATED = 36_050
TESTED = 33_800
SKIPPED = 2_250


def build_manifest() -> dict[str, Any]:
    """Return the main run's CONTRACT-shaped manifest (funnel reconciles to the digit)."""
    return {
        "run_id": RUN_ID,
        "selection": {
            "symbols": ["NSE-NIFTY"],
            "tfs": list(TFS),
            "from": "2020-01-01",
            "to": "2026-07-04",
            "k": 3,
            "directions": list(DIRECTIONS),
        },
        "estimate": {
            "trials": ENUMERATED,
            "est_seconds_provisional": 1_800,
            "est_bytes": 250_000_000,
        },
        "actual": {
            "enumerated": ENUMERATED,
            "tested": TESTED,
            "skipped": SKIPPED,
            "funnel": [{"stage": stage, "count": count} for stage, count in FUNNEL_STAGES],
        },
        "per_stage_timings": [
            {"stage": stage, "seconds": seconds}
            for stage, seconds in (
                ("preflight", 2.125),
                ("sweep", 1_412.5),
                ("gates", 96.25),
                ("rank", 14.5),
                ("refine", 310.75),
                ("cascade", 188.0),
                ("artifacts", 3.5),
            )
        ],
        "per_door_kills": [{"door": door, "killed": killed} for door, killed in DOOR_KILLS],
        "dedup_skips": SKIPPED,
        "tripwires": [],
        "suspect": False,
    }


#: The account-equity base the resilience worst-streak % anchors on (the
#: engine ``grid.start_equity_paisa`` — a stable fixture constant here).
_RES_START_EQUITY_PAISA = 1_000_000
#: The first trade instant (IST 09:15 as UTC — the fold reads IST calendar
#: buckets off exit_ts, deterministic across TZs).
_RES_EPOCH = datetime(2024, 1, 2, 9, 15, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _ResTrade:
    """A ``fold_resilience`` ``_TradeLike`` (frozen fields, not a namespace)."""

    pnl_paisa: int
    entry_ts: datetime
    exit_ts: datetime


def _resilience_block(rank: int) -> dict[str, Any]:
    """The deterministic ``resilience`` block for one entry (the WRITER wraps
    EVERY entry via ``project_resilience_annotation`` — mirrored here).

    Most ranks carry a LIVE block folded from a deterministic per-rank trade
    sequence; the zero-trades edge (``ZERO_TRADES_RANK``) carries the NULL
    block (parity-failed / no trades → ``stats_live: false``, every field
    ``None``), so the display layer's honest "—" degrade is exercised end-to-
    end. All ₹ are GROSS, per-unit (Ruling 18)."""
    if rank == ZERO_TRADES_RANK:
        # A genuine 0-trade finalist → the fold's NULL record (n_trades 0).
        return resilience_payload(
            fold_resilience(
                f"r-{rank}",
                [],
                parity_ok=True,
                start_equity_paisa=_RES_START_EQUITY_PAISA,
                fingerprint=f"fp-{rank}",
            )
        )
    trades: list[_ResTrade] = []
    count = 40 + (rank % 20)  # 40..59 trades — real losing runs + clustering
    for i in range(count):
        pnl_paisa = ((rank * 3 + i * 7) % 13 - 6) * 500  # −3000..3000 paisa
        ts = _RES_EPOCH + timedelta(days=i * 9)  # ~9-day spacing (calendar span)
        trades.append(_ResTrade(pnl_paisa=pnl_paisa, entry_ts=ts, exit_ts=ts))
    return resilience_payload(
        fold_resilience(
            f"r-{rank}",
            trades,
            parity_ok=True,
            start_equity_paisa=_RES_START_EQUITY_PAISA,
            fingerprint=f"fp-{rank}",
        )
    )


def build_top100_rows() -> list[dict[str, Any]]:
    """Return the 100 deterministic contract rows incl. the documented edge-class rows."""
    rows: list[dict[str, Any]] = []
    for rank in range(1, 101):
        idx = rank - 1
        bit = idx % 60
        sharpe = round(3.2 - idx * 0.0173, 10)
        band_lo = 11.0 + (rank % 5) * 0.75
        band_hi = 17.5 + (rank % 7) * 0.5
        row: dict[str, Any] = {
            "rank": rank,
            # The writer's mask form: 32-hex, NO 0x prefix (_MASK_HEX_WIDTH).
            "mask_hex": f"{(1 << bit) | 0x5:032x}",
            "conditions": [f"COND_BIT_{bit:02d}", "PDH_CROSS_UP"],
            "instrument": "NSE-NIFTY",
            "tf": TFS[idx % len(TFS)],
            "direction": DIRECTIONS[idx % len(DIRECTIONS)],
            # The writer's arm DICT (_entry_payload) — the reader bridges it.
            "arm": {
                "sl_paisa": (1 + rank % 3) * 2_575,
                "tp_paisa": (2 + rank % 4) * 3_125,
            },
            "metrics": {
                # ENGINE metric names (the writer's _metrics_payload keys) —
                # the reader's TOP100_METRIC_KEY_MAP adds the display aliases.
                # NO cpcv_pass_pct: the writer emits none (honest "—").
                "net_pnl_paisa": 9_000_000 - rank * 37_531,
                "sharpe_ratio": sharpe,
                "profit_factor": round(2.5 - idx * 0.0113, 10),
                "win_rate": round(0.61 - idx * 0.0023, 10),
                "max_drawdown_paisa": -(150_000 + rank * 8_117),
                "total_trades": 180 + (rank * 7) % 260,
                "deflated_sharpe": round(0.93 - idx * 0.0041, 10),
            },
            # The writer's regime key set (_regime_payload).
            "regime": {
                "by_year": {
                    "2024": 6_000_000 - rank * 25_021,
                    "2025": 3_000_000 - rank * 12_510,
                },
                "by_trend": {
                    "trending": 6_000_000 - rank * 25_021,
                    "sideways": 3_000_000 - rank * 12_510,
                },
                "one_regime_wonder": False,
                "regime_wonder_axis": None,
                "entry_hour_hist": {"9": 100 + rank, "10": 80 + (rank * 3) % 90},
                "event_days": {"counts": {}, "pnl": {}},
            },
            # The writer's vix_band key set (_vix_band_payload, stamped arm).
            "vix_band": {
                "lo": band_lo,
                "hi": band_hi,
                "stamped": 180 + (rank * 7) % 260,
                "missing": 0,
                "win": {"min": band_lo, "avg": round((band_lo + band_hi) / 2, 10), "max": band_hi},
                "loss": {
                    "min": round(band_lo - 0.5, 10),
                    "avg": round((band_lo + band_hi) / 2 - 0.5, 10),
                    "max": round(band_hi - 0.5, 10),
                },
            },
            "family_id": f"fam-{1 + idx // 4:04d}",
            "family_members": [f"fam-{1 + idx // 4:04d}"],
            # Providers unwired in the fixture run (matches source flags).
            "reasons": None,
            "margin_roi": None,
            # The sibling losing-streak resilience block (the writer wraps
            # EVERY entry; a NULL block for the zero-trades edge).
            "resilience": _resilience_block(rank),
        }
        rows.append(row)
    # Metric TIE pair (F-m2 golden): rank 42 shares rank 41's sharpe exactly.
    rows[TIE_RANKS[1] - 1]["metrics"]["sharpe_ratio"] = rows[TIE_RANKS[0] - 1]["metrics"][
        "sharpe_ratio"
    ]
    # Zero-trades edge: an honest all-zero metrics row.
    rows[ZERO_TRADES_RANK - 1]["metrics"].update(
        net_pnl_paisa=0,
        sharpe_ratio=0.0,
        profit_factor=0.0,
        win_rate=0.0,
        max_drawdown_paisa=0,
        total_trades=0,
    )
    # Null-arm edge: the writer's honest None legs (a store row without a
    # tuple arm) — the reader bridges to "SL — · TP —", never a fabrication.
    rows[NULL_ARM_RANK - 1]["arm"] = {"sl_paisa": None, "tp_paisa": None}
    # Negative-PF display edge (impossible in reality — renderer must not crash).
    rows[NEGATIVE_PF_RANK - 1]["metrics"]["profit_factor"] = -0.5
    # Huge-drawdown edge.
    rows[HUGE_DD_RANK - 1]["metrics"]["max_drawdown_paisa"] = -9_999_999_999
    # Null VIX band edge (R31 honest NULL).
    rows[NULL_VIX_RANK - 1]["vix_band"] = None
    return rows


def build_top100_object(
    run_id: str, rows: list[dict[str, Any]], *, pool: int | None = None
) -> dict[str, Any]:
    """Wrap *rows* in the SHIPPED writer's OBJECT root (the ONE fixture shape law).

    Mirrors ``brutex.engine.top100_artifact.Top100Artifact.to_payload`` key
    set exactly: ``{schema_version, run_id, preset, source, baselines,
    entries}`` with the writer's honesty ``source`` header (deterministic
    fixture tallies — pool == gate_passers, nothing excluded, providers
    unwired, matching a plain fixture run).
    """
    n = len(rows) if pool is None else pool
    return {
        "schema_version": TOP100_SCHEMA_VERSION,
        "run_id": run_id,
        "preset": DEFAULT_PRESET,
        "source": {
            "data_snapshot_hashes": ["f" * 64],
            "stores": 3,
            "pool": n,
            "gate_passers": n,
            "excluded_no_aggs": 0,
            "zero_trade_excluded": 0,
            "parity_excluded": 0,
            "families_collapsed": 0,
            "reasons_wired": False,
            "margin_roi_wired": False,
        },
        "baselines": {},
        "entries": rows,
    }


def build_event_bodies(run_id: str = RUN_ID) -> list[dict[str, Any]]:
    """Return ~120 UNhashed event bodies covering every chain-map stage + a tripwire + a checkpoint."""
    bodies: list[dict[str, Any]] = []
    second = 0

    def _ts() -> str:
        nonlocal second
        stamp = f"2026-07-07T04:{30 + second // 60:02d}:{second % 60:02d}Z"
        second += 1
        return stamp

    stages = ("preflight", "sweep", "gates", "rank", "refine", "cascade", "artifacts", "display")
    for stage in stages:
        bodies.append({"ts_utc": _ts(), "run_id": run_id, "kind": f"{stage}.start", "payload": {}})
    for tuple_no in range(1, 101):
        bodies.append(
            {
                "ts_utc": _ts(),
                "run_id": run_id,
                "kind": "sweep.tuple_done",
                "payload": {"tuple": tuple_no, "trials": 338 * tuple_no},
            }
        )
    bodies.append(
        {
            "ts_utc": _ts(),
            "run_id": run_id,
            "kind": "tripwire.count_reconcile",
            "payload": {"note": "enumerated == tested + skipped verified mid-run"},
        }
    )
    bodies.append(
        {
            "ts_utc": _ts(),
            "run_id": run_id,
            "kind": "checkpoint.saved",
            "payload": {"tuples_complete": 100},
        }
    )
    for stage in stages:
        bodies.append({"ts_utc": _ts(), "run_id": run_id, "kind": f"{stage}.done", "payload": {}})
    return bodies


def chain_records(bodies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stamp each body with its chain ``hash`` via the adapter's OWN seam."""
    prev = oneclick_artifacts._CHAIN_GENESIS
    records: list[dict[str, Any]] = []
    for body in bodies:
        digest = chain_hash(prev, canonical_record_bytes(body))
        record = dict(body)
        record["hash"] = digest
        records.append(record)
        prev = digest
    return records


def build_trade_rows() -> list[dict[str, Any]]:
    """Return the 412 deterministic contract trade rows (paisa ints; every 13th VIX null)."""
    rows: list[dict[str, Any]] = []
    for i in range(TRADE_ROW_COUNT):
        pnl_paisa = ((i * 977) % 5_000 - 1_800) * 100
        entry_minute = i * 5
        entry_px = 22_000.0 + (i % 50) * 2.5
        null_vix = i % 13 == 0
        rows.append(
            {
                "entry_ts": f"2026-07-{1 + entry_minute // 1440:02d}T"
                f"{(entry_minute // 60) % 24:02d}:{entry_minute % 60:02d}:00Z",
                "entry_px": entry_px,
                "exit_ts": f"2026-07-{1 + (entry_minute + 4) // 1440:02d}T"
                f"{((entry_minute + 4) // 60) % 24:02d}:{(entry_minute + 4) % 60:02d}:00Z",
                "exit_px": round(entry_px + pnl_paisa / 100.0, 2),
                "direction": DIRECTIONS[i % 2],
                "lots": 1 + i % 3,
                "exit_reason": EXIT_REASONS[i % 4],
                "pnl_paisa": pnl_paisa,
                "costs_paisa": 0,  # Track 1 is signal-only — brokerage assumption: NONE
                "mae": -abs(pnl_paisa) - (i % 7) * 250,
                "mfe": abs(pnl_paisa) + (i % 11) * 175,
                "vix_entry": None if null_vix else round(12.0 + (i % 9) * 0.625, 3),
                "vix_exit": None if null_vix else round(12.25 + (i % 9) * 0.625, 3),
            }
        )
    return rows


def build_hostile_manifest() -> dict[str, Any]:
    """Return the hostile twin's manifest (contract keys + XSS strings + suspect=true)."""
    manifest = build_manifest()
    manifest["run_id"] = HOSTILE_RUN_ID
    manifest["selection"] = {"symbols": list(HOSTILE_STRINGS), "tfs": ["5m"]}
    manifest["tripwires"] = [{"stage": "sweep", "note": HOSTILE_STRINGS[0]}]
    manifest["suspect"] = True
    return manifest


def build_hostile_top100() -> list[dict[str, Any]]:
    """Return 3 contract rows whose string fields carry the hostile payloads."""
    rows = build_top100_rows()[:3]
    for i, row in enumerate(rows):
        row["conditions"] = [HOSTILE_STRINGS[i], "PDH_CROSS_UP"]
        row["family_id"] = "<img src=x onerror=alert(1)>"
    return rows


def build_hostile_event_bodies() -> list[dict[str, Any]]:
    """Return hostile event bodies (XSS strings in ``kind`` + ``payload``)."""
    return [
        {
            "ts_utc": "2026-07-07T05:00:00Z",
            "run_id": HOSTILE_RUN_ID,
            "kind": HOSTILE_STRINGS[0],
            "payload": {"note": HOSTILE_STRINGS[1]},
        },
        {
            "ts_utc": "2026-07-07T05:00:01Z",
            "run_id": HOSTILE_RUN_ID,
            "kind": "sweep.start",
            "payload": {"attr": HOSTILE_STRINGS[2]},
        },
    ]


def _write_json(path: Path, payload: Any) -> None:
    """Write *payload* as pretty-stable JSON (sorted keys, trailing newline)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_events(path: Path, records: list[dict[str, Any]], *, half_written_tail: bool) -> None:
    """Write chained *records* as JSONL; optionally append a half-written last line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for record in records
    ]
    text = "\n".join(lines) + "\n"
    if half_written_tail:
        text += '{"ts_utc":"2026-07-07T05:59:59Z","run_id":"' + CORRUPT_RUN_ID + '","ki'
    path.write_text(text, encoding="utf-8")


def generate(root: Path = FIXTURE_ROOT) -> None:
    """Write the whole corpus under *root* (deterministic — safe to re-run)."""
    runs = root / "sweep_runs"
    main = runs / RUN_ID
    _write_json(main / "manifest.json", build_manifest())
    top100 = build_top100_rows()
    _write_json(main / "top100.json", build_top100_object(RUN_ID, top100))
    _write_events(
        main / "events.jsonl", chain_records(build_event_bodies()), half_written_tail=False
    )
    trades = build_trade_rows()
    _write_json(main / "trades" / "rank-0001.json", {"rows": trades, "total": len(trades)})

    _write_json(runs / EMPTY_RUN_ID / "top100.json", build_top100_object(EMPTY_RUN_ID, []))
    _write_json(
        runs / SHORT_RUN_ID / "top100.json",
        build_top100_object(SHORT_RUN_ID, top100[:SHORT_ROW_COUNT]),
    )

    corrupt = runs / CORRUPT_RUN_ID
    corrupt.mkdir(parents=True, exist_ok=True)
    full_json = json.dumps(build_top100_object(CORRUPT_RUN_ID, top100[:3]), sort_keys=True)
    (corrupt / "top100.json").write_text(full_json[:40], encoding="utf-8")  # truncated JSON
    _write_events(
        corrupt / "events.jsonl",
        chain_records(build_event_bodies(CORRUPT_RUN_ID)[:2]),
        half_written_tail=True,
    )

    hostile = runs / HOSTILE_RUN_ID
    _write_json(hostile / "manifest.json", build_hostile_manifest())
    _write_json(
        hostile / "top100.json", build_top100_object(HOSTILE_RUN_ID, build_hostile_top100())
    )
    _write_events(
        hostile / "events.jsonl",
        chain_records(build_hostile_event_bodies()),
        half_written_tail=False,
    )


if __name__ == "__main__":  # pragma: no cover — regeneration entry point
    generate()
    print(f"one-click fixture corpus regenerated under {FIXTURE_ROOT}")
