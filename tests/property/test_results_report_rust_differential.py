"""Hypothesis differentials for the Rust-hosted results_report render laws.

DEC-RUST-CASCADE-RESULTS-001 phase C (slice 2c). The converted Python law
bodies are DELETED, so each property here drives the Rust-backed shim
(``brutex.engine.results_report``) against a VERBATIM in-test copy of the
pre-conversion Python law frozen from commit bed612533 (the retained
independent-oracle-in-test convention — the slice-2a/2b
``test_*_rust_differential.py`` precedent, R45): hypothesis mutates valid
inputs with hostile edits, BOTH sides run to an OUTCOME, and the outcomes
must be EXACTLY equal — the rendered text on success, the SAME exception
TYPE and FULL ``str`` on failure (raw non-``ValueError`` escapes — a
raising ``__str__``, the ``float(10**400)`` ``OverflowError`` — ARE the
frozen law, so ``BaseException`` is caught on both sides).

Priority targets (the S2C Phase-B divergence-risk roster):

* ``py_fixed`` vs CPython fixed-point formatting — swept over EVERY f64
  bit pattern (``st.integers(0, 2**64-1)`` reinterpreted, so NaN payloads /
  ±inf / subnormals are all in-domain; neither side can raise, so the
  full-plane strategy is generation-safe — the 2b flake lesson) at the
  three production precisions (``+.4f`` via ``_format_deflated``; ``+.3f``
  and ``.1f`` via the leaderboard rows; ``.4f`` via the sidecar pbo).
* ``format_inr`` vs the frozen ``brutex.money.format_inr`` (bed612533
  Decimal law) over the FULL i64 domain including the ``i64::MIN``/``MAX``
  boundaries and beyond-2**53 paisa — integer exactness is the point.
* the sort-field resolution + refusal-message family, the two
  ``_format_*`` helpers, ``_provisional_note`` + ``_render_sidecar_group``
  over JSON-shaped dict strategies, the whole-file
  ``render_cascade_sidecar`` law over hostile documents (version puns
  included), and ``results_show_render``'s text-assembly law.
* multi-defect ORDER shapes: several defects coexisting must surface the
  SAME error first on both sides (the deleted source's evaluation order —
  report-shape before labels, counts before dsr before pbo, the marker's
  ``run_id`` probe before ``instrument``).

Decision (iii) honored: strings never enter Rust — surrogate-carrying
strings ride the Python-assembled fragments, so lone surrogates ARE in the
equality strategies wherever the oracle path admits them (leaderboard
fields, labels, origins values; NOT file contents, which ``write_text``
would refuse on both sides).

Disclosed divergence envelope (scoped OUT of the equality strategies and
pinned separately in ``TestDisclosedEnvelope``):

* ``net_pnl_paisa`` beyond ±i64 refuses LOUDLY (``OverflowError`` at the
  ONE numeric extraction) where the deleted Python law still rendered
  (int64-paisa contract; ``ResultRow`` validates int64 — non-production);
* an object overriding ``__format__`` but NOT ``__str__`` diverges (the
  f-string called ``__format__``; the crate interpolates via ``str()``) —
  unreachable from JSON-minted labels or ``ResultRow``'s str fields;
* non-float-typed metric fields (``win_rate`` / ``sharpe_ratio`` /
  ``deflated_sharpe``) diverge in exception TYPE (crate ``TypeError`` at
  f64 extraction vs the f-string's ``ValueError``/exact-int render) —
  ``ResultRow`` validates them ``float``, so the equality strategies scope
  metric fields to floats (ints additionally differ in multiply order:
  Python multiplies exact ints BEFORE the float-mediated ``.1f``).
"""

from __future__ import annotations

import json
import struct
import tempfile
from collections.abc import Callable, Sequence
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final, cast

import pytest
from brutex.engine.cascade_rust import require_cascade_rust
from brutex.engine.results_report import (
    SORT_ALIASES,
    _format_conditions,
    _format_deflated,
    _provisional_note,
    _render_sidecar_group,
    _resolve_sort_field,
    render_cascade_sidecar,
    render_leaderboard,
)
from brutex.engine.results_store import ResultRow, ResultsStoreError
from hypothesis import given
from hypothesis import strategies as st

_I64_MIN: Final[int] = -(2**63)
_I64_MAX: Final[int] = 2**63 - 1


# ───────────────────────────── outcome helper ────────────────────────────────


def _outcome(fn: Callable[[], object]) -> tuple[object, ...]:
    """Run *fn* to an outcome: ``("ok", value)`` or ``("exc", type, str)``.

    ``BaseException`` is deliberate — raw-escape parity (a raising
    ``__str__``, the ``OverflowError`` float coercions) IS the property.
    """
    try:
        return ("ok", fn())
    except BaseException as exc:  # outcome parity is the property
        return ("exc", type(exc), str(exc))


# ───────────────── VERBATIM pre-conversion oracles (bed612533) ───────────────

#: Frozen ``brutex.money`` law (bed612533) — the leaderboard's money render.
_MONEY_QUANTUM: Final[Decimal] = Decimal("0.01")
_PAISA_PER_RUPEE: Final[Decimal] = Decimal(100)


def _oracle_round_money(rupees: Decimal) -> Decimal:
    """VERBATIM bed612533 ``brutex.money.round_money``."""
    return rupees.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _oracle_paisa_to_rupees(paisa: int) -> Decimal:
    """VERBATIM bed612533 ``brutex.money.paisa_to_rupees``."""
    return _oracle_round_money(Decimal(paisa) / _PAISA_PER_RUPEE)


def _oracle_format_inr(paisa: int) -> str:
    """VERBATIM bed612533 ``brutex.money.format_inr``."""
    return f"₹{_oracle_paisa_to_rupees(paisa):,.2f}"


def _oracle_resolve_sort_field(sort_by: str) -> str:
    """VERBATIM bed612533 ``_resolve_sort_field`` (unchanged type faces imported)."""
    field = SORT_ALIASES.get(sort_by)
    if field is None:
        raise ResultsStoreError(
            f"unknown sort key {sort_by!r}; choose one of {sorted(SORT_ALIASES)}"
        )
    return field


def _oracle_format_deflated(value: float | None) -> str:
    """VERBATIM bed612533 ``_format_deflated``."""
    if value is None:
        return "—"
    return f"{value:+.4f}"


def _oracle_format_conditions(condition_names: str) -> str:
    """VERBATIM bed612533 ``_format_conditions``."""
    if not condition_names:
        return "(no conditions — enters every bar)"
    return condition_names


def _oracle_render_leaderboard(
    rows: Sequence[Any], *, origins: dict[str, str] | None = None
) -> str:
    """VERBATIM bed612533 ``render_leaderboard`` body (docstring elided)."""
    if not rows:
        return "(no persisted results)"
    lines: list[str] = []
    for rank, row in enumerate(rows, start=1):
        marker = "" if origins is None else f"  src={origins.get(row.run_id, '?')}"
        lines.append(
            f"#{rank}  {row.instrument} {row.tf} {row.direction}  "
            f"pnl={_oracle_format_inr(row.net_pnl_paisa)}  "
            f"trades={row.total_trades}  "
            f"win={row.win_rate * 100:.1f}%  "
            f"sharpe={row.sharpe_ratio:+.3f}  "
            f"dsr@tupleN={_oracle_format_deflated(row.deflated_sharpe)}{marker}\n"
            f"    {_oracle_format_conditions(row.condition_names)}"
        )
    return "\n".join(lines)


def _oracle_provisional_note(report: dict[str, object]) -> str:
    """VERBATIM bed612533 ``_provisional_note`` body."""
    verdicts = report.get("verdicts")
    if not isinstance(verdicts, list):
        return ""
    stamped = [v for v in verdicts if isinstance(v, dict) and "provisional" in v]
    if not stamped:
        return ""
    n_provisional = sum(
        1 for v in stamped if v.get("provisional") is True and v.get("passed") is True
    )
    if n_provisional == 0:
        return ""
    return f"  provisional={n_provisional} (pool-N DSR pass — not proven at leaderboard N)"


def _oracle_render_sidecar_group(entry: object) -> str:
    """VERBATIM bed612533 ``_render_sidecar_group`` body."""
    if not isinstance(entry, dict):
        raise ValueError("group entry is not an object")
    report = entry.get("report")
    if not isinstance(report, dict):
        raise ValueError("group report is not an object")
    label = f"{entry.get('instrument', '?')} {entry.get('tf', '?')}"
    counts = f"in={report.get('n_input', '?')} survived={report.get('n_survived', '?')}"
    if "dsr_n_source" not in report:
        dsr = "dsr N not stamped (pre-ADR-0008 report)"
    else:
        dsr = f"dsr N={report.get('dsr_n_trials', '?')} source={report['dsr_n_source']}"
    dsr += _oracle_provisional_note(report)
    if "pool_pbo" not in report:
        return f"  {label}  pool PBO not recorded (pre-sidecar report)  {counts}  {dsr}"
    pbo = report["pool_pbo"]
    if pbo is None:
        return (
            f"  {label}  pool PBO skipped (pool < 2 candidates / short series / "
            f"pool empty at #5)  {counts}  {dsr}"
        )
    return f"  {label}  pool PBO={float(pbo):.4f}  {counts}  {dsr}"


def _oracle_render_cascade_sidecar(root: Path) -> str:
    """VERBATIM bed612533 ``render_cascade_sidecar`` body (unchanged faces imported)."""
    from brutex.engine.cascade_wire import REPORT_VERSION, report_path

    path = report_path(root)
    if not path.is_file():
        return ""
    header = f"Stage-4 cascade sidecar — {path}"
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"{header}\n  UNREADABLE: {exc}"
    if not isinstance(payload, dict) or not isinstance(payload.get("groups"), list):
        return f"{header}\n  UNREADABLE: not a cascade report object"
    version = payload.get("version")
    if version != REPORT_VERSION:
        return f"{header}\n  UNREADABLE: unsupported report version {version!r}"
    try:
        groups = [_oracle_render_sidecar_group(entry) for entry in payload["groups"]]
    except ValueError as exc:
        return f"{header}\n  UNREADABLE: {exc}"
    if not groups:
        return f"{header}\n  (no pooled slices)"
    return "\n".join([header, *groups])


def _oracle_show_render(
    root: object,
    files_n: int,
    finalists_n: int,
    sort_by: object,
    showing_n: int,
    leaders: Sequence[Any],
    origins: dict[str, str] | None,
    sidecar: str,
) -> str:
    """VERBATIM bed612533 ``results_show_report`` text-assembly tail.

    The orchestration half (top check · sort resolve · discovery · merge ·
    top_k · origins build · sidecar file read) stays shim-side Python and is
    corpus-replayed; this oracle is the assembly tail's exact lines over the
    same pre-computed inputs the crate's ``show_render`` receives.
    """
    if origins is None:
        provenance_note = (
            "  (Stage-2 grid rows are gate survivors; a --stage3-enable sweep also "
            "persists UNGATED Stage-3 refined-eval trials — no per-row gate verdict)"
        )
    else:
        provenance_note = (
            "  (per-row src= markers from the cascade manifest: src=grid rows are "
            "gate survivors; src=stage3 rows are UNGATED refined trials — "
            "DEC-ROW-PROVENANCE-001)"
        )
    header = (
        f"BruteX results — {root}\n"
        f"  files={files_n}  finalists={finalists_n}  "
        f"sort={sort_by}  showing top {showing_n}\n"
        f"{provenance_note}"
    )
    body = f"{header}\n\n{_oracle_render_leaderboard(leaders, origins=origins)}"
    return f"{body}\n\n{sidecar}" if sidecar else body


# ─────────────────────────────── strategies ──────────────────────────────────


def _bits_to_float(bits: int) -> float:
    return cast("float", struct.unpack("<d", struct.pack("<Q", bits))[0])


#: EVERY f64 bit pattern — NaN payloads (both sign bits), ±inf, subnormals,
#: -0.0 all in-domain. Neither format law raises on any of them, so this is
#: generation-safe under the default profile (the 2b flake lesson).
_all_floats = st.integers(min_value=0, max_value=2**64 - 1).map(_bits_to_float)

#: Metric floats for the leaderboard rows — the full plane plus the exact
#: hostile spellings (ties / display-underflow / huge).
_metric_floats = st.one_of(
    _all_floats,
    st.sampled_from([0.0, -0.0, 0.25, 0.75, 0.15625, 0.09375, 1e-7, 5e-324, 1e300, 0.99999]),
)

_i64 = st.integers(min_value=_I64_MIN, max_value=_I64_MAX)

#: Hostile text — lone surrogates / astral / whitespace-only / marker-shaped
#: fragments beside plain generated text (``st.text()`` excludes surrogates
#: by default, so the hostile spellings are sampled explicitly).
_hostile_text = st.one_of(
    st.text(max_size=12),
    st.sampled_from(
        ["", " ", "\t", "\n", "a\ud800b", "\ud800", "\U0001d54a", "src=", "₹", "0", "  src=?"]
    ),
)

_run_ids = st.one_of(st.sampled_from(["r1", "r2", "dup"]), st.text(max_size=8))


def _duck_row(**overrides: Any) -> Any:
    """A duck-typed row (the crate reads via getattr, the oracle via f-string)."""
    base: dict[str, Any] = {
        "run_id": "r1",
        "instrument": "NIFTY",
        "tf": "5m",
        "direction": "bull",
        "net_pnl_paisa": 1_000,
        "total_trades": 40,
        "win_rate": 0.625,
        "sharpe_ratio": 1.5,
        "deflated_sharpe": 0.97,
        "condition_names": "rsi_oversold + above_vwap",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


_duck_rows = st.fixed_dictionaries(
    {
        "run_id": _run_ids,
        "instrument": _hostile_text,
        "tf": _hostile_text,
        "direction": _hostile_text,
        "net_pnl_paisa": _i64,
        # NEVER extracted — interpolated via str(); unbounded ints are legal.
        "total_trades": st.integers(),
        "win_rate": _metric_floats,
        "sharpe_ratio": _metric_floats,
        "deflated_sharpe": st.one_of(st.none(), _metric_floats),
        "condition_names": _hostile_text,
    }
).map(lambda kw: SimpleNamespace(**kw))

_origins = st.one_of(
    st.none(),
    st.dictionaries(
        _run_ids,
        st.one_of(st.sampled_from(["grid", "stage3"]), _hostile_text, st.integers(), st.none()),
        max_size=4,
    ),
)

#: Sidecar-group label values — the ``.get(key, '?')`` fallback fires on
#: ABSENCE only, so present ``None``/int/bool labels are echoed via str().
_labels = st.one_of(_hostile_text, st.integers(), st.none(), st.booleans())

#: ``float(pbo)`` coercion domain — CPython's own ``float()`` runs on both
#: sides, so the full literal grammar (underscores, inf/nan spellings,
#: whitespace, non-ASCII Nd digits) and the exact ValueError/OverflowError
#: messages are shared by construction.
_pbo_values = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**6), max_value=10**6),
    _all_floats,
    st.sampled_from(
        ["1.5", " 2 ", "2\n", "inf", "-inf", "nan", "+nan", "1_0", "1__0", "junk", "", "0x10", "١٢"]
    ),
    st.lists(st.integers(), max_size=2),
    st.just(10**400),  # float() OverflowError — propagates RAW on both sides
)

_verdict_values = st.sampled_from([True, False, 1, 0, "x", None])
_verdict_entries = st.one_of(
    st.none(),
    st.integers(),
    st.text(max_size=4),
    st.dictionaries(
        st.sampled_from(["provisional", "passed", "candidate_id"]), _verdict_values, max_size=3
    ),
)
_verdicts = st.one_of(
    st.none(),
    st.integers(),
    st.text(max_size=4),
    st.tuples(_verdict_entries),  # a TUPLE is not a list → ""
    st.lists(_verdict_entries, max_size=6),
)

_group_reports = st.fixed_dictionaries(
    {},
    optional={
        "n_input": _labels,
        "n_survived": _labels,
        "dsr_n_source": st.one_of(
            st.sampled_from(["leaderboard", "pool"]), _hostile_text, st.none(), st.integers()
        ),
        "dsr_n_trials": _labels,
        "pool_pbo": _pbo_values,
        "verdicts": _verdicts,
    },
)

_group_entries = st.one_of(
    st.none(),
    st.integers(),
    st.text(max_size=4),
    st.lists(st.integers(), max_size=1),
    st.fixed_dictionaries(
        {},
        optional={
            "instrument": _labels,
            "tf": _labels,
            "report": st.one_of(st.none(), st.text(max_size=4), _group_reports),
        },
    ),
)

# JSON-safe subsets for the whole-document sidecar render (must survive
# json.dumps + write_text: finite floats, no surrogates, no huge ints).
_json_labels = st.one_of(
    st.text(max_size=8),
    st.integers(min_value=-(10**6), max_value=10**6),
    st.none(),
    st.booleans(),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
)
_json_group_reports = st.fixed_dictionaries(
    {},
    optional={
        "n_input": _json_labels,
        "n_survived": _json_labels,
        "dsr_n_source": st.one_of(st.sampled_from(["leaderboard", "pool"]), _json_labels),
        "dsr_n_trials": _json_labels,
        "pool_pbo": st.one_of(
            st.none(),
            st.booleans(),
            st.integers(min_value=-(10**6), max_value=10**6),
            st.floats(allow_nan=False, allow_infinity=False),
            st.sampled_from(["1.5", "inf", "junk", ""]),
            st.lists(st.integers(), max_size=1),
        ),
        "verdicts": st.one_of(
            st.none(),
            st.lists(
                st.one_of(
                    st.none(),
                    st.integers(max_value=10**6, min_value=-(10**6)),
                    st.dictionaries(
                        st.sampled_from(["provisional", "passed"]),
                        st.sampled_from([True, False, 1, 0, None]),
                        max_size=2,
                    ),
                ),
                max_size=4,
            ),
        ),
    },
)
_json_group_entries = st.one_of(
    st.none(),
    st.integers(min_value=-10, max_value=10),
    st.text(max_size=4),
    st.fixed_dictionaries(
        {},
        optional={
            "instrument": _json_labels,
            "tf": _json_labels,
            "report": st.one_of(st.none(), st.text(max_size=4), _json_group_reports),
        },
    ),
)

#: Whole sidecar documents: structured reports (version puns included —
#: ``True == 1`` LOADS), raw scalars, and junk text.
_sidecar_docs = st.one_of(
    st.fixed_dictionaries(
        {},
        optional={
            "version": st.sampled_from([1, True, False, 1.0, "1", None, 99, 2]),
            "groups": st.one_of(
                st.lists(_json_group_entries, max_size=4),
                st.none(),
                st.text(max_size=4),
                st.integers(min_value=-10, max_value=10),
            ),
            "extra": st.integers(min_value=-10, max_value=10),
        },
    ).map(json.dumps),
    st.text(max_size=40),
    st.sampled_from(
        ['{"version": 1, "groups": []}', "null", "[]", '"str"', "{", "12", "", "{}\n{}"]
    ),
)


def _as_rows(rows: Sequence[Any]) -> Sequence[ResultRow]:
    """Duck rows are the deliberate domain widening — cast for the shim signature."""
    return cast("Sequence[ResultRow]", rows)


def _as_origins(origins: dict[str, Any] | None) -> dict[str, str] | None:
    """Origins values are str()-interpolated on both sides — cast the widened map."""
    return cast("dict[str, str] | None", origins)


# ─────────────────────────── py_fixed / _format_deflated ─────────────────────


class TestFormatDeflatedDifferential:
    """``py_fixed(v, 4, plus=True)`` vs CPython ``f"{v:+.4f}"`` — every bit pattern."""

    @given(value=st.one_of(st.none(), _all_floats))
    def test_matches_the_deleted_law(self, value: float | None) -> None:
        assert _format_deflated(value) == _oracle_format_deflated(value)

    @pytest.mark.parametrize(
        "value",
        [
            0.0,
            -0.0,
            5e-324,
            -5e-324,
            1.7976931348623157e308,
            -1.7976931348623157e308,
            float("inf"),
            float("-inf"),
            float("nan"),
            _bits_to_float(0xFFF8000000000000),  # -nan (sign-bit NaN)
            _bits_to_float(0x7FF8000000000001),  # NaN payload
            0.15625,
            0.09375,
            2.00005,
            1e-7,
            1e18,
        ],
    )
    def test_special_value_pins(self, value: float) -> None:
        assert _format_deflated(value) == _oracle_format_deflated(value)


# ─────────────────────────────── format_inr ──────────────────────────────────


class TestFormatInrDifferential:
    """The crate's integer money twin vs the frozen Decimal ``format_inr``."""

    @given(paisa=_i64)
    def test_money_fragment_matches_the_frozen_law(self, paisa: int) -> None:
        row = _duck_row(net_pnl_paisa=paisa)
        got = render_leaderboard(_as_rows([row]))
        assert got == _oracle_render_leaderboard([row])
        assert f"pnl={_oracle_format_inr(paisa)}  trades=" in got

    @pytest.mark.parametrize(
        "paisa",
        [
            _I64_MIN,
            _I64_MAX,
            0,
            1,
            -1,
            99,
            -99,
            100,
            -100,
            101,
            999,
            10**7,  # ₹1,00,000-scale (lakh in paisa — Western grouping law)
            10**9 * 100,  # crore-scale rupees
            2**53,
            2**53 + 1,
            -(2**53) - 1,
            9_007_199_254_740_993,
            2**62,
            -(2**62),
        ],
    )
    def test_boundary_pins(self, paisa: int) -> None:
        row = _duck_row(net_pnl_paisa=paisa)
        got = render_leaderboard(_as_rows([row]))
        assert f"pnl={_oracle_format_inr(paisa)}  trades=" in got
        assert got == _oracle_render_leaderboard([row])


# ─────────────────────────── leaderboard render law ──────────────────────────


class TestLeaderboardDifferential:
    """Whole-table parity over hostile rows + origins (marker default law)."""

    @given(rows=st.lists(_duck_rows, max_size=5), origins=_origins)
    def test_matches_the_deleted_law(self, rows: list[Any], origins: dict[str, Any] | None) -> None:
        got = render_leaderboard(_as_rows(rows), origins=_as_origins(origins))
        want = _oracle_render_leaderboard(rows, origins=_as_origins(origins))
        assert got == want

    def test_empty_rows_placeholder(self) -> None:
        assert render_leaderboard(_as_rows([])) == _oracle_render_leaderboard([])

    def test_missing_run_id_renders_question_mark_default(self) -> None:
        row = _duck_row(run_id="absent-from-origins")
        got = render_leaderboard(_as_rows([row]), origins={})
        assert "  src=?" in got
        assert got == _oracle_render_leaderboard([row], origins={})


# ─────────────────────────── sort-field resolution ───────────────────────────


class TestResolveSortFieldDifferential:
    """Aliases-only vocabulary + the verbatim repr-carrying refusal message."""

    @given(
        token=st.one_of(
            st.sampled_from(
                sorted(SORT_ALIASES)
                + sorted(set(SORT_ALIASES.values()))
                + ["", "SHARPE", "sharpe ", " sharpe", "shar\ud800pe"]
            ),
            _hostile_text,
        )
    )
    def test_matches_the_deleted_law(self, token: str) -> None:
        got = _outcome(lambda: _resolve_sort_field(token))
        want = _outcome(lambda: _oracle_resolve_sort_field(token))
        assert got == want
        if got[0] == "exc":
            assert got[1] is ResultsStoreError


# ─────────────────────────── _format_conditions ──────────────────────────────


class TestFormatConditionsDifferential:
    """Falsy-empty note + verbatim truthy passthrough (object identity)."""

    @given(value=_hostile_text)
    def test_matches_the_deleted_law(self, value: str) -> None:
        assert _format_conditions(value) == _oracle_format_conditions(value)

    @given(value=_hostile_text.filter(bool))
    def test_truthy_passthrough_is_the_original_object(self, value: str) -> None:
        assert _format_conditions(value) is value


# ─────────────────────────── _provisional_note ───────────────────────────────


class TestProvisionalNoteDifferential:
    """The strict ``is True`` identity law (int ``1`` never counts, either key)."""

    @given(report=st.fixed_dictionaries({}, optional={"verdicts": _verdicts}))
    def test_matches_the_deleted_law(self, report: dict[str, object]) -> None:
        assert _provisional_note(report) == _oracle_provisional_note(report)

    @pytest.mark.parametrize(
        "verdicts",
        [
            [{"provisional": 1, "passed": True}],  # int 1 is NOT True (identity)
            [{"provisional": True, "passed": 1}],
            [{"provisional": True, "passed": True}],
            [{"provisional": True}],  # passed absent → .get None → not counted
            [{"provisional": False, "passed": True}, {"provisional": True, "passed": True}],
            ({"provisional": True, "passed": True},),  # tuple → not a list → ""
        ],
    )
    def test_identity_law_pins(self, verdicts: object) -> None:
        report = {"verdicts": verdicts}
        assert _provisional_note(report) == _oracle_provisional_note(report)


# ─────────────────────────── _render_sidecar_group ───────────────────────────


class TestSidecarGroupDifferential:
    """One group line — shape refusals, label fallbacks, float(pbo) coercion."""

    @given(entry=_group_entries)
    def test_matches_the_deleted_law(self, entry: object) -> None:
        got = _outcome(lambda: _render_sidecar_group(entry))
        want = _outcome(lambda: _oracle_render_sidecar_group(entry))
        assert got == want

    @pytest.mark.parametrize(
        "pbo",
        ["junk", "", "1.5", " 2 ", "inf", "nan", "1_0", "1__0", "0x10", "١٢", 10**400, [1], True],
    )
    def test_float_coercion_grammar_pins(self, pbo: object) -> None:
        entry = {"instrument": "N", "tf": "5m", "report": {"pool_pbo": pbo}}
        got = _outcome(lambda: _render_sidecar_group(entry))
        want = _outcome(lambda: _oracle_render_sidecar_group(entry))
        assert got == want


# ───────────────────────── whole-file sidecar render ─────────────────────────


class TestSidecarFileRenderDifferential:
    """``render_cascade_sidecar`` (shim split read + crate render) vs the
    verbatim one-try oracle over the SAME on-disk document."""

    @staticmethod
    def _both(content: str | None) -> tuple[tuple[object, ...], tuple[object, ...]]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            if content is not None:
                (root / "cascade").mkdir()
                (root / "cascade" / "cascade-report.json").write_text(content, encoding="utf-8")
            got = _outcome(lambda: render_cascade_sidecar(root))
            want = _outcome(lambda: _oracle_render_cascade_sidecar(root))
        return got, want

    @given(content=_sidecar_docs)
    def test_matches_the_deleted_law(self, content: str) -> None:
        got, want = self._both(content)
        assert got == want

    def test_absent_file_is_empty_both_sides(self) -> None:
        got, want = self._both(None)
        assert got == want == ("ok", "")

    def test_deep_nesting_bomb_escapes_raw_both_sides(self) -> None:
        """A RecursionError is NOT in the ``(OSError, ValueError)`` catch set —
        it escapes RAW from json.loads on both sides (the frozen escape law)."""
        got, want = self._both("[" * 200_000)
        assert got[0] == want[0] == "exc"
        assert got[1] is RecursionError
        assert want[1] is RecursionError


# ───────────────────────── show_render text assembly ─────────────────────────


class TestShowRenderDifferential:
    """The header · provenance-note · leaderboard · sidecar assembly law."""

    @given(
        root=_hostile_text,
        files_n=st.integers(min_value=0, max_value=10_000),
        finalists_n=st.integers(min_value=0, max_value=10_000),
        sort_by=_hostile_text,
        rows=st.lists(_duck_rows, max_size=3),
        origins=_origins,
        sidecar=st.one_of(st.just(""), _hostile_text),
    )
    def test_matches_the_deleted_law(
        self,
        root: str,
        files_n: int,
        finalists_n: int,
        sort_by: str,
        rows: list[Any],
        origins: dict[str, Any] | None,
        sidecar: str,
    ) -> None:
        got = require_cascade_rust().results_show_render(
            root, files_n, finalists_n, sort_by, len(rows), _as_rows(rows), origins, sidecar
        )
        want = _oracle_show_render(
            root, files_n, finalists_n, sort_by, len(rows), rows, _as_origins(origins), sidecar
        )
        assert got == want


# ───────────────────────── multi-defect construction order ───────────────────


class _RaisingStr:
    """Raises identically from BOTH interpolation routes (f-string calls
    ``__format__``; the crate calls ``str()``) so the surfaced error is the
    same object either way — the ordering, not the route, is the property."""

    def __init__(self, tag: str) -> None:
        self._tag = tag

    def __str__(self) -> str:
        raise RuntimeError(f"boom-{self._tag}")

    def __format__(self, spec: str) -> str:
        raise RuntimeError(f"boom-{self._tag}")


class _RaisingGetDict(dict[str, Any]):
    """A dict whose ``.get`` raises — reachable only via direct calls (JSON
    can never mint it); pins WHICH stage of the group render runs first."""

    def get(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom-get")


class TestMultiDefectConstructionOrder:
    """Several defects coexisting must surface the SAME error first."""

    def test_report_shape_check_beats_label_defect(self) -> None:
        entry = {"instrument": _RaisingStr("instrument"), "report": "junk"}
        got = _outcome(lambda: _render_sidecar_group(entry))
        want = _outcome(lambda: _oracle_render_sidecar_group(entry))
        assert got == want == ("exc", ValueError, "group report is not an object")

    def test_label_defect_beats_counts_defect(self) -> None:
        entry = {
            "instrument": _RaisingStr("instrument"),
            "report": {"n_input": _RaisingStr("counts"), "pool_pbo": "junk"},
        }
        got = _outcome(lambda: _render_sidecar_group(entry))
        want = _outcome(lambda: _oracle_render_sidecar_group(entry))
        assert got == want == ("exc", RuntimeError, "boom-instrument")

    def test_counts_defect_beats_junk_pbo(self) -> None:
        entry = {"report": {"n_input": _RaisingStr("counts"), "pool_pbo": "junk"}}
        got = _outcome(lambda: _render_sidecar_group(entry))
        want = _outcome(lambda: _oracle_render_sidecar_group(entry))
        assert got == want == ("exc", RuntimeError, "boom-counts")

    def test_dsr_trials_defect_beats_junk_pbo(self) -> None:
        entry = {
            "report": {
                "dsr_n_source": "pool",
                "dsr_n_trials": _RaisingStr("trials"),
                "pool_pbo": "junk",
            }
        }
        got = _outcome(lambda: _render_sidecar_group(entry))
        want = _outcome(lambda: _oracle_render_sidecar_group(entry))
        assert got == want == ("exc", RuntimeError, "boom-trials")

    def test_provisional_note_defect_beats_junk_pbo(self) -> None:
        entry = {
            "report": {
                "verdicts": [_RaisingGetDict({"provisional": True})],
                "pool_pbo": "junk",
            }
        }
        got = _outcome(lambda: _render_sidecar_group(entry))
        want = _outcome(lambda: _oracle_render_sidecar_group(entry))
        assert got == want == ("exc", RuntimeError, "boom-get")

    def test_marker_run_id_probe_beats_instrument_read(self) -> None:
        """The marker precedes the row f-string in source — with origins the
        missing ``run_id`` surfaces; without, the missing ``instrument``."""
        row = SimpleNamespace()  # NO attributes at all
        with_origins = _outcome(lambda: render_leaderboard(_as_rows([row]), origins={}))
        want_with = _outcome(lambda: _oracle_render_leaderboard([row], origins={}))
        assert with_origins == want_with
        assert with_origins[1] is AttributeError
        assert "run_id" in cast("str", with_origins[2])
        without = _outcome(lambda: render_leaderboard(_as_rows([row])))
        want_without = _outcome(lambda: _oracle_render_leaderboard([row]))
        assert without == want_without
        assert "instrument" in cast("str", without[2])


# ───────────────────────── disclosed divergence envelope ─────────────────────


class TestDisclosedEnvelope:
    """The scoped-out divergences, pinned so they stay KNOWN + disclosed."""

    def test_beyond_i64_pnl_refuses_where_python_rendered(self) -> None:
        """The ONE numeric extraction: beyond-±i64 ``net_pnl_paisa`` refuses
        LOUDLY (OverflowError) where the frozen Python law still rendered —
        unreachable from validated ``ResultRow`` int64 columns (disclosed).
        The Python law's OWN ceiling sat higher: past ~1e28 paisa the frozen
        Decimal context refused too (``InvalidOperation``) — so beyond i64
        BOTH sides refuse eventually, with different types + thresholds."""
        for beyond in (2**63, -(2**63) - 1, 2**80):
            row = _duck_row(net_pnl_paisa=beyond)

            def _shim(r: Any = row) -> str:
                return render_leaderboard(_as_rows([r]))

            def _oracle(r: Any = row) -> str:
                return _oracle_render_leaderboard([r])

            got = _outcome(_shim)
            assert got[0] == "exc"
            assert got[1] is OverflowError
            want = _outcome(_oracle)
            assert want[0] == "ok"
        # Past the frozen Decimal context's ~1e28-paisa ceiling the OLD law
        # refused as well — InvalidOperation, not OverflowError.
        from decimal import InvalidOperation

        far = _outcome(lambda: _oracle_render_leaderboard([_duck_row(net_pnl_paisa=10**30)]))
        assert far[0] == "exc"
        assert far[1] is InvalidOperation
        # Exactly AT the i64 boundaries both sides still render with parity.
        for edge in (_I64_MAX, _I64_MIN):
            row = _duck_row(net_pnl_paisa=edge)
            assert render_leaderboard(_as_rows([row])) == _oracle_render_leaderboard([row])
        assert "pnl=₹92,233,720,368,547,758.07" in render_leaderboard(
            _as_rows([_duck_row(net_pnl_paisa=_I64_MAX)])
        )

    def test_format_dunder_override_diverges_disclosed(self) -> None:
        """An object overriding ``__format__`` but NOT ``__str__`` diverges:
        the f-string called ``__format__``; the crate interpolates ``str()``.
        Unreachable from JSON-minted labels or ResultRow's str fields —
        json.loads can never mint such an object (disclosed, non-production)."""

        class FormatNotStr:
            def __format__(self, spec: str) -> str:
                return "FORMAT"

            def __str__(self) -> str:
                return "STR"

        row = _duck_row(instrument=FormatNotStr())
        got = render_leaderboard(_as_rows([row]))
        want = _oracle_render_leaderboard([row])
        assert "#1  STR 5m" in got
        assert "#1  FORMAT 5m" in want
        assert got != want  # the disclosed class — flips loudly if ever closed
