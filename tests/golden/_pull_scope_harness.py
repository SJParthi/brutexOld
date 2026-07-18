"""Shared harness for the pull-scope note-surgery goldens (DEC-RUST-PULL-STATE-001).

Computes the frozen surgery tables from the LIVE ``brutex.dashboard.pull_scope``
module — imported by BOTH the generator (`generate_pull_scope_golden.py`) and
the consuming test (`test_pull_scope_golden.py`), never generator-imports-test
(the ``_market_hours_guard_harness`` shape).

The corpus inputs are REAL shapes wherever a real producer exists: the
completed-chain notes are driven through the actual ``run_full_pipeline`` →
``pipeline_pull._complete_note`` chain over the scoped no-op reports (the
exact-string producer the surgery anchors couple to), the deferral note
through the real ``pipeline_pull._market_hours_stop_note``, and the spot-stop
note through a genuinely gate-failing spot report. Synthetic drift cases
(anchors absent / one-of-two) cover the ``note_surgery_miss`` WARN arm + the
pass-through law.

Two renderings of ONE table:

* ``compute_note_surgery_table()`` — the JSON-shaped Python oracle table.
* ``render_rust_fixture(table)`` — the flat escaped-TSV twin the dependency-
  free ``cargo test`` consumes via ``include_str!`` (the crate has no JSON
  parser and Cargo.toml deps are hub-frozen). The consuming Python test pins
  the TSV byte-equal to the re-rendered JSON table, so the two committed
  fixtures are ONE oracle logically.
"""

from __future__ import annotations

from typing import Any

from structlog.testing import capture_logs

from brutex.dashboard.pipeline_pull import (
    _market_hours_stop_note,
    run_full_pipeline,
)
from brutex.dashboard.composite_pull import CompositePullReport
from brutex.dashboard.metadata_pull import MetadataPullReport
from brutex.dashboard.pull_controller import BulkSpotReport, SymbolPullResult
from brutex.dashboard.pull_scope import (
    GREEKS_NOT_RUN_NOTE,
    CrossverifyTailOutcome,
    build_fno_only_note,
    build_spot_only_note,
    crossverify_clause,
    fno_only_spot_report,
    normalize_pull_scope,
    rewrite_market_hours_note,
    spot_only_historical_report,
    spot_only_metadata_report,
)

#: The fixed tail every spot-surgery case appends (status/human recorded in
#: the fixture so the pre-append derivation is self-describing).
FIXED_TAIL = CrossverifyTailOutcome(
    status="started", human="producer started (poll /api/v1/crossverify/progress)"
)

#: Schema version of the fixture payloads (bump = a conscious re-freeze).
SCHEMA_VERSION = 1

#: The anchor/constant names frozen into the fixture — the cross-language
#: drift-pin surface (Python consts == fixture == Rust consts).
ANCHOR_NAMES: tuple[str, ...] = (
    "SCOPE_FULL",
    "SCOPE_SPOT_ONLY",
    "SCOPE_FNO_ONLY",
    "GREEKS_NOT_RUN_NOTE",
    "FNO_SKIPPED_CLAUSE",
    "SPOT_SKIPPED_CLAUSE",
    "_NOOP_SPOT_CLAUSE",
    "_NOOP_FNO_CLAUSE",
    "_STOP_FNO_CLAUSE",
    "_STOP_FNO_SPOT_ONLY_CLAUSE",
    "_FULL_PREFIX",
    "_SPOT_ONLY_PREFIX",
    "_FNO_ONLY_PREFIX",
    "_MARKET_HOURS_WINDOW_CLAUSE",
    "_MARKET_HOURS_WINDOW_SPOT_ONLY_CLAUSE",
    "_MARKET_HOURS_PHASES_CLAUSE",
    "_MARKET_HOURS_PHASES_SPOT_ONLY_CLAUSE",
    "_MARKET_HOURS_PHASES_FNO_ONLY_CLAUSE",
    "_MARKET_HOURS_RESUME_CLAUSE",
    "_MARKET_HOURS_RESUME_SPOT_ONLY_CLAUSE",
)


def _anchors() -> dict[str, str]:
    """Every frozen anchor literal, read off the LIVE module by name."""
    from brutex.dashboard import pull_scope as mod

    return {name: getattr(mod, name) for name in ANCHOR_NAMES}


# ── real producers (the exact-string coupling surface) ───────────────────────


def _spot_report(
    *,
    symbols: list[SymbolPullResult] | None = None,
    ledger_load_failed: bool = False,
) -> BulkSpotReport:
    return BulkSpotReport(
        dry_run=False,
        start_date="2020-01-01",
        end_date="2026-07-12",
        symbols=(
            [SymbolPullResult(symbol="NIFTY", completed=1, failed=0, no_data=0)]
            if symbols is None
            else symbols
        ),
        ledger_load_failed=ledger_load_failed,
    )


def _sequencer_note(spot: BulkSpotReport) -> str:
    """The REAL sequencer note over the spot-only no-op reports."""
    report = run_full_pipeline(
        run_spot=lambda: spot,
        run_metadata=spot_only_metadata_report,
        run_historical=lambda _prior: spot_only_historical_report(),
    )
    assert report.stopped_after is None
    return report.note


def _spot_stop_note_real() -> str:
    """The REAL spot-gate stop note (``_spot_stop_note`` via the sequencer)."""
    failing_spot = BulkSpotReport(
        dry_run=False,
        start_date="2020-01-01",
        end_date="2026-07-12",
        symbols=[SymbolPullResult(symbol="NIFTY", completed=0, failed=0, no_data=0)],
        still_failed_windows=3,
    )
    report = run_full_pipeline(
        run_spot=lambda: failing_spot,
        run_metadata=spot_only_metadata_report,
        run_historical=lambda _prior: spot_only_historical_report(),
    )
    assert report.stopped_after == "spot"
    return report.note


def _fno_metadata_report() -> MetadataPullReport:
    return MetadataPullReport(
        dry_run=False,
        years=(2024,),
        underlyings=("NIFTY",),
        expiry_cells=1,
        cells_skipped=0,
        expiry_failures=0,
        expiries_persisted=4,
        contract_jobs=1,
        contract_failures=0,
        contracts_persisted=40,
        note="offline fake",
    )


def _fno_historical_report() -> CompositePullReport:
    return CompositePullReport(
        dry_run=False,
        requested=("NIFTY",),
        tradable=("NIFTY",),
        skipped_reference_only=(),
        metadata=None,
        historical=None,
        note="offline fake",
    )


def _fno_sequencer_note() -> str:
    """The REAL sequencer note over the fno-only no-op SPOT report."""
    report = run_full_pipeline(
        run_spot=fno_only_spot_report,
        run_metadata=_fno_metadata_report,
        run_historical=lambda _prior: _fno_historical_report(),
    )
    assert report.stopped_after is None
    return report.note


# ── corpus ───────────────────────────────────────────────────────────────────


def _scope_corpus() -> list[str | None]:
    """The ``normalize_pull_scope`` token corpus.

    Covers blank/None defaults, the known vocabulary (verbatim + padded with
    ASCII, Unicode AND the CPython-``str.strip``-only C0 separators
    U+001C..U+001F — the classic Rust ``char::is_whitespace`` parity gap),
    and the refusal family (case drift, underscore drift, embedded NUL,
    em-dash homoglyph).
    """
    return [
        None,
        "",
        "   ",
        "\t",
        "\n  \n",
        "full",
        "spot-only",
        "fno-only",
        "  spot-only  ",
        "\tfno-only\t",
        " full ",
        "\x1cfull\x1d",  # CPython str.strip-only whitespace (bidi B class)
        "\x1espot-only\x1f",  # more of the U+001C..U+001F parity set
        "\x85spot-only\xa0",  # NEL + NBSP — shared White_Space set
        " fno-only　",  # em space + ideographic space
        "spot_only",
        "SPOT-ONLY",
        "spot-only extra",
        "options",
        "spot—only",  # em-dash homoglyph — refused
        "spot-only\x00",  # NUL is NOT whitespace — refused
        "N",  # the TSV None-marker letter as a literal token — refused (never punned)
    ]


def _mh_rewrite_cases() -> list[dict[str, Any]]:
    """(note, phases kind) cases for ``rewrite_market_hours_note``."""
    real = _market_hours_stop_note()
    once = rewrite_market_hours_note(real)
    custom = "custom phases wording — with an em-dash"
    cases: list[dict[str, Any]] = [
        {"note": real, "phases": "SPOT"},
        {"note": real, "phases": "FNO"},
        {"note": once, "phases": "SPOT"},  # idempotence on the rewritten note
        {"note": "deferred — some future wording with no anchors", "phases": "SPOT"},
        {"note": "please resume on the next out-of-hours run soon", "phases": "SPOT"},
        {"note": real, "phases": f"C:{custom}"},
        {"note": "", "phases": "SPOT"},
    ]
    return cases


def _spot_surgery_corpus() -> list[str]:
    anchors = _anchors()
    return [
        _sequencer_note(_spot_report()),
        _sequencer_note(_spot_report(ledger_load_failed=True)),
        _sequencer_note(
            _spot_report(
                symbols=[
                    SymbolPullResult(symbol="NIFTY", completed=1, failed=0, no_data=0),
                    SymbolPullResult(symbol="BADSYM", completed=0, failed=1, no_data=0),
                ]
            )
        ),
        _sequencer_note(
            _spot_report(
                symbols=[SymbolPullResult(symbol="NIFTY", completed=1, failed=0, no_data=0)]
                + [
                    SymbolPullResult(symbol=f"STOCK{i:02d}", completed=0, failed=1, no_data=0)
                    for i in range(12)
                ]
            )
        ),
        _market_hours_stop_note(),
        _spot_stop_note_real(),
        # half drift: prefix present, F&O clause drifted away → WARN arm.
        "pipeline complete — spot clean (1 pair(s)), metadata went sideways",
        # reverse half drift: clause present, prefix drifted away → WARN arm.
        "totally new prefix " + anchors["_NOOP_FNO_CLAUSE"],
        # full drift (a metadata stop note — neither anchor, no WARN).
        "stopped after metadata — 1 expiry + 0 contract failure(s); historical NOT started",
        "",
    ]


def _fno_surgery_corpus() -> list[str]:
    anchors = _anchors()
    ledger_hist = CompositePullReport(
        dry_run=False,
        requested=("NIFTY",),
        tradable=("NIFTY",),
        skipped_reference_only=(),
        metadata=None,
        historical=None,
        note="offline fake",
    )
    _ = ledger_hist  # the ledger-fail arm is driven via the spot report below
    ledger_fail_note = run_full_pipeline(
        run_spot=lambda: BulkSpotReport(
            dry_run=False,
            start_date="",
            end_date="",
            symbols=[],
            ledger_load_failed=True,
        ),
        run_metadata=_fno_metadata_report,
        run_historical=lambda _prior: _fno_historical_report(),
    ).note
    return [
        _fno_sequencer_note(),
        ledger_fail_note,
        _market_hours_stop_note(),
        # half drift: prefix present, spot clause drifted away → WARN arm.
        "pipeline complete — spot wording moved, metadata 4 expiries + 40 contracts",
        # reverse half drift: clause present, prefix drifted away → WARN arm.
        "totally new prefix " + anchors["_NOOP_SPOT_CLAUSE"],
        # full drift (a metadata stop — TRUE trailer here, no surgery, no WARN).
        "stopped after metadata — 0 expiry + 1 contract failure(s); historical NOT started",
        "",
    ]


# ── table computation (the oracle capture) ───────────────────────────────────


def _scope_row(token: str | None) -> dict[str, Any]:
    row: dict[str, Any] = {"token": token}
    try:
        row["ok"] = normalize_pull_scope(token)
    except ValueError as exc:
        row["error"] = str(exc)
    return row


def _phases_replacement(kind: str) -> str:
    from brutex.dashboard import pull_scope as mod

    if kind == "SPOT":
        return mod._MARKET_HOURS_PHASES_SPOT_ONLY_CLAUSE
    if kind == "FNO":
        return mod._MARKET_HOURS_PHASES_FNO_ONLY_CLAUSE
    assert kind.startswith("C:")
    return kind[2:]


def _mh_row(case: dict[str, Any]) -> dict[str, Any]:
    replacement = _phases_replacement(case["phases"])
    return {
        "note": case["note"],
        "phases": case["phases"],
        "expected": rewrite_market_hours_note(case["note"], phases_replacement=replacement),
    }


def _surgery_flags(logs: list[dict[str, Any]]) -> tuple[bool, bool | None, bool | None]:
    """(miss_warned, prefix_present, clause_present) off the captured WARN."""
    miss = [e for e in logs if e["event"] == "pull_scope.note_surgery_miss"]
    if not miss:
        return False, None, None
    entry = miss[0]
    clause = entry.get("fno_clause_present", entry.get("spot_clause_present"))
    return True, bool(entry["prefix_present"]), bool(clause)


def _spot_row(base_note: str) -> dict[str, Any]:
    with capture_logs() as logs:
        full = build_spot_only_note(base_note, FIXED_TAIL)
    miss_warned, warn_prefix, warn_clause = _surgery_flags(logs)
    suffix = f", {GREEKS_NOT_RUN_NOTE}{crossverify_clause(FIXED_TAIL)}"
    assert full.endswith(suffix), "step-3 append law moved — re-derive the harness"
    surgery_only = full[: -len(suffix)]
    # The kernel-visible booleans: recomputed the module's own way on the
    # POST-market-hours-rewrite, POST-stop-clause note (the transcription law).
    from brutex.dashboard import pull_scope as mod

    probe = rewrite_market_hours_note(base_note)
    if mod._STOP_FNO_CLAUSE in probe:
        probe = probe.replace(mod._STOP_FNO_CLAUSE, mod._STOP_FNO_SPOT_ONLY_CLAUSE, 1)
    prefix_present = mod._FULL_PREFIX in probe
    clause_present = mod._NOOP_FNO_CLAUSE in probe
    if miss_warned:
        assert (warn_prefix, warn_clause) == (prefix_present, clause_present)
    return {
        "base_note": base_note,
        "full": full,
        "surgery_only": surgery_only,
        "prefix_present": prefix_present,
        "clause_present": clause_present,
        "miss_warned": miss_warned,
    }


def _fno_row(base_note: str) -> dict[str, Any]:
    with capture_logs() as logs:
        note = build_fno_only_note(base_note)
    miss_warned, warn_prefix, warn_clause = _surgery_flags(logs)
    from brutex.dashboard import pull_scope as mod

    probe = rewrite_market_hours_note(
        base_note, phases_replacement=mod._MARKET_HOURS_PHASES_FNO_ONLY_CLAUSE
    )
    prefix_present = mod._FULL_PREFIX in probe
    clause_present = mod._NOOP_SPOT_CLAUSE in probe
    if miss_warned:
        assert (warn_prefix, warn_clause) == (prefix_present, clause_present)
    return {
        "base_note": base_note,
        "note": note,
        "prefix_present": prefix_present,
        "clause_present": clause_present,
        "miss_warned": miss_warned,
    }


def compute_note_surgery_table() -> dict[str, Any]:
    """The whole frozen oracle table, computed from the LIVE module."""
    return {
        "schema_version": SCHEMA_VERSION,
        "tail": {"status": FIXED_TAIL.status, "human": FIXED_TAIL.human},
        "anchors": _anchors(),
        "scope": [_scope_row(token) for token in _scope_corpus()],
        "market_hours_rewrite": [_mh_row(case) for case in _mh_rewrite_cases()],
        "spot_only_surgery": [_spot_row(note) for note in _spot_surgery_corpus()],
        "fno_only_surgery": [_fno_row(note) for note in _fno_surgery_corpus()],
    }


# ── the flat TSV twin (dependency-free cargo consumption) ────────────────────

_SCOPE_CODES = {"full": 0, "spot-only": 1, "fno-only": 2}


def escape_field(value: str) -> str:
    """Escape a fixture string for one TSV field (Rust unescapes the mirror).

    Backslash escapes for the separators + backslash itself; ``\\u{XXXX}``
    (Rust-literal shape) for the remaining C0/C1 controls so every byte of
    the fixture file is printable UTF-8. Everything else — em-dashes
    included — stays raw UTF-8 (the byte-exactness surface under test).
    """
    out: list[str] = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F or 0x80 <= ord(ch) < 0xA0:
            out.append(f"\\u{{{ord(ch):04x}}}")
        else:
            out.append(ch)
    return "".join(out)


def render_rust_fixture(table: dict[str, Any]) -> str:
    """Render the flat escaped-TSV twin of *table* for ``include_str!``."""
    lines: list[str] = [
        "# pull_scope note-surgery golden — the flat twin of",
        "# tests/golden/fixtures/pull_scope_note_surgery.json (ONE generator,",
        "# cross-format-pinned by tests/golden/test_pull_scope_golden.py).",
        f"# schema_version={table['schema_version']}",
    ]
    # Canonical ANCHOR_NAMES order (NOT dict iteration order — the JSON
    # fixture round-trips through sort_keys=True, so a rendering off the
    # loaded dict must not depend on insertion order).
    for name in ANCHOR_NAMES:
        lines.append(f"A\t{name}\t{escape_field(table['anchors'][name])}")
    for row in table["scope"]:
        # Prefix marker (unpunnable): ``N`` = the Python ``None`` token;
        # ``T<escaped>`` = a string token (any literal, sentinel-proof).
        token = "N" if row["token"] is None else "T" + escape_field(row["token"])
        code = 255 if "error" in row else _SCOPE_CODES[row["ok"]]
        lines.append(f"S\t{token}\t{code}")
    for row in table["market_hours_rewrite"]:
        phases = row["phases"]
        phases_field = phases if phases in {"SPOT", "FNO"} else "C:" + escape_field(phases[2:])
        lines.append(
            f"M\t{escape_field(row['note'])}\t{phases_field}\t{escape_field(row['expected'])}"
        )
    for row in table["spot_only_surgery"]:
        lines.append(
            "P\t"
            f"{escape_field(row['base_note'])}\t{escape_field(row['surgery_only'])}\t"
            f"{int(row['prefix_present'])}\t{int(row['clause_present'])}"
        )
    for row in table["fno_only_surgery"]:
        lines.append(
            "F\t"
            f"{escape_field(row['base_note'])}\t{escape_field(row['note'])}\t"
            f"{int(row['prefix_present'])}\t{int(row['clause_present'])}"
        )
    return "\n".join(lines) + "\n"
