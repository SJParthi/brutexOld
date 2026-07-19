//! The `brutex.engine.top100_artifact` builder/reader LAW family
//! (DEC-RUST-CASCADE-RESULTS-001 phase B, slice 2b).
//!
//! Ported token-for-token from the DELETED Python law bodies and verified
//! against the FROZEN pre-conversion parity corpus
//! (`tests/golden/fixtures/cascade_rust_parity/top100_artifact.json`,
//! 86 cases) plus the sha256-locked `test_top100_artifact_golden.py` bytes.
//!
//! ## The cut (recorded in the slice-2b handoff)
//!
//! * **Crate** — the `_dumps` byte law ([`crate::pyjson::dumps_sorted_surrogatepass`]),
//!   the 5-hard-gates predicate, the whole `build_top100` pass (dup refusal →
//!   preset resolution → cascade sweep_id gate → gate filter → family
//!   collapse → rank delegation → entry projection → honesty header →
//!   artifact construction → `top100.built`), the `_loads_object` belt and
//!   the `load_top100` validation ladder over already-read bytes.
//! * **Shim** — the type face (dataclasses + their `__post_init__` raises +
//!   constants), file I/O (`atomic_write_simple` writes, the two
//!   `read_bytes` arms and their `stamp_unreadable`/`artifact_unreadable`
//!   reasons, the stamp's wall-clock `produced_at_utc`), the
//!   `top100.load_invalid` / `top100.written` log emissions, and the
//!   ranking/presets delegation (`brutex.ranking` is a hard carve-out).
//!
//! ## Consumed laws passed AS DATA (never re-minted here)
//!
//! `resolve_preset` (the preset vocabulary + its `top100.unknown_preset`
//! WARN), `rank_candidates` (the `sort_by_preset` two-pass ordering law),
//! `matched_condition_names` (the vocabulary describe law), the
//! `Top100Error` / `Top100Artifact` classes, the module structlog logger,
//! the gate constants (cross-pinned against `brutex.ranking`'s privates
//! shim-side) and the `_STAMP_KEYS` / `ENTRY_REQUIRED_KEYS` frozensets.
//!
//! ## Construction-order fidelity (the slice-2a MINOR-1 lesson, baked in)
//!
//! Every observable effect keeps the deleted law's program order: the
//! duplicate-run_id refusal walks the pool BEFORE `resolve_preset` can WARN;
//! the sweep_id-mismatch WARN precedes the gate filter; per ENTRY the
//! provider calls run BEFORE the cascade payload, which runs BEFORE the
//! dict-literal walk — so on a multi-defect candidate the `worst5_days`
//! `json.loads` raises BEFORE the regime columns, and the regime columns
//! raise in their literal order (`by_year` → `by_trend` → `entry_hour_hist`
//! → event counts → event pnl). The raw `JSONDecodeError` escapes UNWRAPPED
//! (CPython's own `json.loads` is called — the frozen quirk-2 law) while the
//! `_loads_object` non-object refusal raises the REAL `Top100Error` class
//! (passed in) with the repr-carrying message.
//!
//! ## Python-semantics FFI notes
//!
//! Equality gates use `rich_compare(…, Ne)` — the two frozen bool==int PUNS
//! (`schema_version: true` and entry `rank: true` both legally LOAD) fall
//! out of Python's own `__ne__`; membership uses `__contains__`; truthiness
//! uses `is_truthy` (`if verdict.passed:` / `not preset`); every payload
//! value crosses as the ORIGINAL PyObject (a NaN aggregate float survives
//! the build and refuses only at render — the frozen quirk-1 law);
//! `hashlib` / `json` are CPython's own via `py.import`.

use std::collections::{BTreeSet, HashMap, HashSet};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::pyclass::CompareOp;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};

use crate::capture_repr;

// ── Law constants (frozen by the corpus constants block) ────────────────────

/// The root `cascade_join.state` vocabulary (writer side).
const STATE_JOINED: &str = "joined";
const STATE_NO_REPORT: &str = "no_report";
const STATE_SWEEP_ID_MISMATCH: &str = "sweep_id_mismatch";

/// sha256 hex digest length (stamp validation).
const SHA256_HEX_LEN: usize = 64;

/// The u128 `mask:%032x` hex width (the run-identity convention).
const MASK_HEX_WIDTH: usize = 32;

// ── helpers ─────────────────────────────────────────────────────────────────

/// Raise the passed domain-error class (`Top100Error`) with a fully-composed
/// message — the real Python exception instance, so `type(exc).__name__` and
/// `str(exc)` are byte-identical to the deleted law's direct raises.
fn domain_err(error_cls: &Bound<'_, PyAny>, msg: String) -> PyErr {
    match error_cls.call1((msg,)) {
        Ok(inst) => PyErr::from_value(inst),
        Err(err) => err,
    }
}

/// Python `a != b` (`rich_compare` — bool==int puns included).
fn py_ne(a: &Bound<'_, PyAny>, b: &Bound<'_, PyAny>) -> PyResult<bool> {
    a.rich_compare(b, CompareOp::Ne)?.is_truthy()
}

/// `json.loads` guarded exactly like the deleted `except ValueError` arms:
/// a `ValueError` (JSONDecodeError / UnicodeDecodeError included) yields
/// `None`; any OTHER exception propagates raw (the original never caught it).
fn loads_or_none<'py>(
    py: Python<'py>,
    json_loads: &Bound<'py, PyAny>,
    raw: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    match json_loads.call1((raw,)) {
        Ok(v) => Ok(Some(v)),
        Err(err) if err.is_instance_of::<PyValueError>(py) => Ok(None),
        Err(err) => Err(err),
    }
}

/// The `_loads_object` law: parse one agg JSON column; a raw
/// `JSONDecodeError` escapes UNWRAPPED, a valid-JSON non-object raises the
/// real `Top100Error` with the repr-carrying message.
pub fn loads_object<'py>(
    json_loads: &Bound<'py, PyAny>,
    rendered: &Bound<'py, PyAny>,
    error_cls: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let payload = json_loads.call1((rendered,))?;
    if !payload.is_instance_of::<PyDict>() {
        return Err(domain_err(
            error_cls,
            format!(
                "agg JSON column did not decode to an object: {}",
                capture_repr(rendered)
            ),
        ));
    }
    Ok(payload)
}

/// The 5 gate constants, threaded from the shim (single source stays the
/// Python constants, cross-pinned against `brutex.ranking`'s privates).
pub struct Gates {
    pub min_trades: i64,
    pub min_profit_factor: f64,
    pub max_drawdown_pct: f64,
    pub min_win_rate: f64,
    pub min_win_loss_ratio: f64,
    pub max_consecutive_losses: i64,
}

impl Gates {
    pub fn from_tuple(t: (i64, f64, f64, f64, f64, i64)) -> Self {
        Self {
            min_trades: t.0,
            min_profit_factor: t.1,
            max_drawdown_pct: t.2,
            min_win_rate: t.3,
            min_win_loss_ratio: t.4,
            max_consecutive_losses: t.5,
        }
    }
}

/// `passes_hard_gates(row, agg)` — the exact recomputed-gate law, reads in
/// the deleted body's order (IEEE comparisons — `30.0` passes, the next
/// double fails; a `None` `win_loss_ratio` contributes `0.0`).
pub fn passes_hard_gates(
    row: &Bound<'_, PyAny>,
    agg: &Bound<'_, PyAny>,
    gates: &Gates,
) -> PyResult<bool> {
    let total_trades: i64 = row.getattr("total_trades")?.extract()?;
    if total_trades < gates.min_trades {
        return Ok(false);
    }
    let profit_factor: f64 = row.getattr("profit_factor")?.extract()?;
    if profit_factor < gates.min_profit_factor {
        return Ok(false);
    }
    let max_drawdown_pct: f64 = row.getattr("max_drawdown_pct")?.extract()?;
    if max_drawdown_pct > gates.max_drawdown_pct {
        return Ok(false);
    }
    let wlr_obj = agg.getattr("win_loss_ratio")?;
    let wlr: f64 = if wlr_obj.is_none() {
        0.0
    } else {
        wlr_obj.extract()?
    };
    let win_rate: f64 = row.getattr("win_rate")?.extract()?;
    if win_rate < gates.min_win_rate && wlr < gates.min_win_loss_ratio {
        return Ok(false);
    }
    let max_losses: i64 = agg.getattr("max_consecutive_losses")?.extract()?;
    Ok(max_losses <= gates.max_consecutive_losses)
}

// ── entry projection ────────────────────────────────────────────────────────

/// `_metrics_payload` — every ResultRow metric + the agg depth stats, in the
/// deleted dict literal's exact insertion order; values cross as the
/// ORIGINAL PyObjects (full precision, ints exact, prices via CPython
/// `str(Decimal)`), `worst5_days` via CPython `json.loads` (raw escape).
#[allow(clippy::too_many_arguments)]
fn metrics_payload<'py>(
    py: Python<'py>,
    row: &Bound<'py, PyAny>,
    agg: &Bound<'py, PyAny>,
    cpcv_pass_rate: &Bound<'py, PyAny>,
    str_f: &Bound<'py, PyAny>,
    json_loads: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let payload = PyDict::new(py);
    for key in [
        "net_pnl_paisa",
        "gross_profit_paisa",
        "gross_loss_paisa",
        "max_drawdown_paisa",
        "expectancy_paisa",
        "total_trades",
        "winning_trades",
        "losing_trades",
        "sharpe_ratio",
        "sortino_ratio",
        "calmar_ratio",
        "profit_factor",
        "win_rate",
        "max_drawdown_pct",
        "deflated_sharpe",
    ] {
        payload.set_item(key, row.getattr(key)?)?;
    }
    payload.set_item(
        "avg_entry_price",
        str_f.call1((row.getattr("avg_entry_price")?,))?,
    )?;
    payload.set_item(
        "avg_exit_price",
        str_f.call1((row.getattr("avg_exit_price")?,))?,
    )?;
    for key in [
        "composite_score",
        "win_loss_ratio",
        "recovery_factor",
        "max_consecutive_losses",
        "max_consecutive_wins",
        "mae_avg_paisa",
        "mae_worst_paisa",
        "mae_p95_paisa",
        "mfe_avg_paisa",
        "mfe_best_paisa",
        "mfe_p95_paisa",
        "mae_1m_avg_paisa",
        "mae_1m_worst_paisa",
        "mae_1m_p95_paisa",
        "mfe_1m_avg_paisa",
        "mfe_1m_best_paisa",
        "mfe_1m_p95_paisa",
        "trades_with_1m",
        "max_dd_minute_path_paisa",
        "max_dd_minute_path_pct",
        "equity_status",
    ] {
        payload.set_item(key, agg.getattr(key)?)?;
    }
    payload.set_item(
        "worst5_days",
        json_loads.call1((agg.getattr("worst5_days_json")?,))?,
    )?;
    let exits = PyDict::new(py);
    exits.set_item("stop_loss", agg.getattr("exits_stop_loss")?)?;
    exits.set_item("take_profit", agg.getattr("exits_take_profit")?)?;
    exits.set_item("signal", agg.getattr("exits_signal")?)?;
    exits.set_item("forced_1520", agg.getattr("exits_forced_1520")?)?;
    exits.set_item("end_of_data", agg.getattr("exits_end_of_data")?)?;
    exits.set_item("session_gap", agg.getattr("exits_session_gap")?)?;
    payload.set_item("exits", exits)?;
    payload.set_item("fingerprint", agg.getattr("fingerprint")?)?;
    if !cpcv_pass_rate.is_none() {
        // The EXACT float unit projection `rate * 100.0` — Python's own `*`
        // (rate 1/3 → 33.33333333333333, the frozen shortest-repr product).
        payload.set_item("cpcv_pass_pct", cpcv_pass_rate.mul(100.0f64)?)?;
    }
    Ok(payload)
}

/// `_regime_payload` — the R46/R51 regime block; the `_loads_object` calls
/// run in the deleted literal's order (`by_year` → `by_trend` →
/// `entry_hour_hist` → event counts → event pnl).
fn regime_payload<'py>(
    py: Python<'py>,
    agg: &Bound<'py, PyAny>,
    json_loads: &Bound<'py, PyAny>,
    error_cls: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let payload = PyDict::new(py);
    payload.set_item(
        "by_year",
        loads_object(json_loads, &agg.getattr("pnl_by_year_json")?, error_cls)?,
    )?;
    payload.set_item(
        "by_trend",
        loads_object(json_loads, &agg.getattr("pnl_by_trend_json")?, error_cls)?,
    )?;
    payload.set_item("one_regime_wonder", agg.getattr("one_regime_wonder")?)?;
    payload.set_item("regime_wonder_axis", agg.getattr("regime_wonder_axis")?)?;
    payload.set_item(
        "entry_hour_hist",
        loads_object(json_loads, &agg.getattr("entry_hour_hist_json")?, error_cls)?,
    )?;
    let event_days = PyDict::new(py);
    event_days.set_item(
        "counts",
        loads_object(
            json_loads,
            &agg.getattr("event_day_counts_json")?,
            error_cls,
        )?,
    )?;
    event_days.set_item(
        "pnl",
        loads_object(json_loads, &agg.getattr("event_day_pnl_json")?, error_cls)?,
    )?;
    payload.set_item("event_days", event_days)?;
    Ok(payload)
}

/// `_vix_band_payload` — band + win/loss stats, honest `None` when any leg
/// of a trio is unstamped (the `is not None` triple-conjunction law).
fn vix_band_payload<'py>(py: Python<'py>, agg: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    let win_min = agg.getattr("vix_win_min")?;
    let win_avg = agg.getattr("vix_win_avg")?;
    let win_max = agg.getattr("vix_win_max")?;
    let win: Option<Bound<'py, PyDict>> =
        if !win_min.is_none() && !win_avg.is_none() && !win_max.is_none() {
            let d = PyDict::new(py);
            d.set_item("min", win_min)?;
            d.set_item("avg", win_avg)?;
            d.set_item("max", win_max)?;
            Some(d)
        } else {
            None
        };
    let loss_min = agg.getattr("vix_loss_min")?;
    let loss_avg = agg.getattr("vix_loss_avg")?;
    let loss_max = agg.getattr("vix_loss_max")?;
    let loss: Option<Bound<'py, PyDict>> =
        if !loss_min.is_none() && !loss_avg.is_none() && !loss_max.is_none() {
            let d = PyDict::new(py);
            d.set_item("min", loss_min)?;
            d.set_item("avg", loss_avg)?;
            d.set_item("max", loss_max)?;
            Some(d)
        } else {
            None
        };
    let payload = PyDict::new(py);
    payload.set_item("lo", agg.getattr("vix_band_lo")?)?;
    payload.set_item("hi", agg.getattr("vix_band_hi")?)?;
    payload.set_item("stamped", agg.getattr("vix_stamped_trades")?)?;
    payload.set_item("missing", agg.getattr("vix_missing_trades")?)?;
    payload.set_item("win", win)?;
    payload.set_item("loss", loss)?;
    Ok(payload)
}

/// `_cascade_payload` — one entry's cascade object + its joined CPCV rate
/// object (Python `None` when unknown). The annotation NEVER reranks.
pub fn cascade_payload<'py>(
    py: Python<'py>,
    run_obj: &Bound<'py, PyAny>,
    join: Option<&Bound<'py, PyAny>>,
    state: &str,
) -> PyResult<(Bound<'py, PyDict>, Bound<'py, PyAny>)> {
    let none = py.None().into_bound(py);
    let not_validated = |reason: &str| -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        d.set_item("status", "not_validated")?;
        d.set_item("reason", reason)?;
        Ok(d)
    };
    let Some(join) = join else {
        return Ok((not_validated(state)?, none));
    };
    if state != STATE_JOINED {
        return Ok((not_validated(state)?, none));
    }
    if join.getattr("ambiguous_ids")?.contains(run_obj)? {
        return Ok((not_validated("duplicate_candidate_id")?, none));
    }
    let verdict = join
        .getattr("by_candidate")?
        .call_method1("get", (run_obj,))?;
    if verdict.is_none() {
        return Ok((not_validated("not_in_cascade_pool")?, none));
    }
    let rate = verdict.getattr("cpcv_pass_rate")?;
    let d = PyDict::new(py);
    if verdict.getattr("passed")?.is_truthy()? {
        let provisional = verdict.getattr("provisional")?;
        let status = if provisional.is_truthy()? {
            "provisional_pass"
        } else {
            "pass"
        };
        d.set_item("status", status)?;
        d.set_item("rejected_at", &none)?;
        d.set_item("detail", verdict.getattr("detail")?)?;
        d.set_item("cascade_composite", verdict.getattr("cascade_composite")?)?;
        d.set_item("provisional", provisional)?;
        d.set_item("cpcv_pass_rate", &rate)?;
        d.set_item("survivor_rank", verdict.getattr("survivor_rank")?)?;
    } else {
        d.set_item("status", "fail")?;
        d.set_item("rejected_at", verdict.getattr("rejected_at")?)?;
        d.set_item("detail", verdict.getattr("detail")?)?;
        d.set_item("cascade_composite", &none)?;
        d.set_item("provisional", false)?;
        d.set_item("cpcv_pass_rate", &rate)?;
        d.set_item("survivor_rank", &none)?;
    }
    Ok((d, rate))
}

// ── builder ─────────────────────────────────────────────────────────────────

/// The consumed-law context threaded from the shim (module docstring).
pub struct BuildCtx<'py> {
    pub resolve_preset_fn: Bound<'py, PyAny>,
    pub rank_fn: Bound<'py, PyAny>,
    pub cond_names_fn: Bound<'py, PyAny>,
    pub error_cls: Bound<'py, PyAny>,
    pub artifact_cls: Bound<'py, PyAny>,
    pub logger: Bound<'py, PyAny>,
    pub gates: Gates,
    pub limit: usize,
    pub schema_version: i64,
}

/// `build_top100` — the whole deleted pass, program-order-faithful.
#[allow(clippy::too_many_arguments)]
pub fn build_top100<'py>(
    py: Python<'py>,
    candidates: &Bound<'py, PyAny>,
    run_id: &Bound<'py, PyAny>,
    preset: &Bound<'py, PyAny>,
    families: &Bound<'py, PyAny>,
    counts: &Bound<'py, PyAny>,
    data_snapshot_hashes: &Bound<'py, PyAny>,
    baselines: &Bound<'py, PyAny>,
    reason_provider: Option<&Bound<'py, PyAny>>,
    margin_roi_provider: Option<&Bound<'py, PyAny>>,
    cascade_join: Option<&Bound<'py, PyAny>>,
    ctx: &BuildCtx<'py>,
) -> PyResult<Bound<'py, PyAny>> {
    let builtins = py.import("builtins")?;
    let list_f = builtins.getattr("list")?;
    let dict_f = builtins.getattr("dict")?;
    let str_f = builtins.getattr("str")?;
    let json_loads = py.import("json")?.getattr("loads")?;
    let none = py.None().into_bound(py);

    // 1. Duplicate-run_id refusal (one identity → one result) — FIRST, so a
    //    dup raises before resolve_preset can WARN (the deleted order).
    let mut cands: Vec<Bound<'py, PyAny>> = Vec::new();
    let mut run_ids: Vec<String> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    for item in candidates.try_iter()? {
        let cand = item?;
        let run_obj = cand.getattr("row")?.getattr("run_id")?;
        let run: String = run_obj.extract()?;
        if !seen.insert(run.clone()) {
            return Err(domain_err(
                &ctx.error_cls,
                format!(
                    "build_top100: duplicate candidate run_id {}",
                    capture_repr(&run_obj)
                ),
            ));
        }
        run_ids.push(run);
        cands.push(cand);
    }

    // 2. Preset resolution (the shim's `resolve_preset` — its own WARN law).
    let resolved = ctx.resolve_preset_fn.call1((preset,))?;

    // 3. Cascade sweep_id gate — a mismatched join drops LOUDLY, entries
    //    stay not_validated.
    let (join_state, join_for_entries): (&str, Option<&Bound<'py, PyAny>>) = match cascade_join {
        None => (STATE_NO_REPORT, None),
        Some(join) => {
            let sweep_id = join.getattr("sweep_id")?;
            if py_ne(&sweep_id, run_id)? {
                let kwargs = PyDict::new(py);
                kwargs.set_item("run_id", run_id)?;
                kwargs.set_item("report_sweep_id", &sweep_id)?;
                kwargs.set_item("note", "stale cascade report — entries stay not_validated")?;
                ctx.logger.call_method(
                    "warning",
                    ("top100.cascade_sweep_id_mismatch",),
                    Some(&kwargs),
                )?;
                (STATE_SWEEP_ID_MISMATCH, None)
            } else {
                (STATE_JOINED, Some(join))
            }
        }
    };

    // 4. Gate filter (candidate order preserved).
    let mut passers: Vec<usize> = Vec::new();
    for (i, cand) in cands.iter().enumerate() {
        let row = cand.getattr("row")?;
        let agg = cand.getattr("agg")?;
        if passes_hard_gates(&row, &agg, &ctx.gates)? {
            passers.push(i);
        }
    }

    // 5. Family collapse (R34) — `by_family` in FIRST-SEEN order (Python
    //    dict setdefault order), one representative per family.
    let fam_map = families.getattr("family_by_run_id")?;
    let members_map = families.getattr("members_by_family")?;
    let mut fam_order: Vec<String> = Vec::new();
    let mut fam_members: HashMap<String, Vec<usize>> = HashMap::new();
    for &i in &passers {
        let run = &run_ids[i];
        let fam_obj = fam_map.call_method1("get", (run.as_str(), run.as_str()))?;
        let family_id: String = fam_obj.extract()?;
        if !fam_members.contains_key(&family_id) {
            fam_order.push(family_id.clone());
        }
        fam_members.entry(family_id).or_default().push(i);
    }
    let mut representatives: Vec<usize> = Vec::new();
    let mut members_of: HashMap<String, Bound<'py, PyAny>> = HashMap::new();
    for family_id in &fam_order {
        let members = &fam_members[family_id];
        // Python `min(members, key=(0 if run==family_id else 1, bit_count,
        // run_id))` — first strictly-smallest wins (stable on ties).
        let mut best: Option<(u8, u32, usize)> = None;
        for &i in members {
            let mask: u128 = cands[i].getattr("row")?.getattr("mask_bits")?.extract()?;
            let prefer: u8 = if &run_ids[i] == family_id { 0 } else { 1 };
            let key = (prefer, mask.count_ones(), i);
            let better = match &best {
                None => true,
                Some(b) => (key.0, key.1, &run_ids[key.2]) < (b.0, b.1, &run_ids[b.2]),
            };
            if better {
                best = Some(key);
            }
        }
        let chosen = best.map(|b| b.2).ok_or_else(|| {
            PyValueError::new_err("internal: empty family member list (unreachable)")
        })?;
        let listed: Bound<'py, PyAny> = if members_map.contains(family_id.as_str())? {
            members_map.get_item(family_id.as_str())?
        } else {
            let mut sorted_runs: Vec<&String> = members.iter().map(|&i| &run_ids[i]).collect();
            sorted_runs.sort();
            PyTuple::new(py, sorted_runs)?.into_any()
        };
        representatives.push(chosen);
        members_of.insert(run_ids[chosen].clone(), listed);
    }
    let families_collapsed = passers.len() - representatives.len();

    // 6. Rank — DELEGATED to the shim's `rank_candidates` (the presets
    //    carve-out; input order = the collapse insertion order, so the
    //    NaN-composite frozen order is preserved), then `[:limit]`.
    let reps_list = PyList::empty(py);
    for &i in &representatives {
        reps_list.append(&cands[i])?;
    }
    let rank_kwargs = PyDict::new(py);
    rank_kwargs.set_item("preset", &resolved)?;
    let ranked_all = ctx.rank_fn.call((reps_list,), Some(&rank_kwargs))?;
    let mut ranked: Vec<Bound<'py, PyAny>> = Vec::new();
    for item in ranked_all.try_iter()? {
        if ranked.len() >= ctx.limit {
            break;
        }
        ranked.push(item?);
    }

    // 7. Entry projection — per entry the deleted call-site order: family
    //    lookup → members → reasons provider → margin provider → cascade
    //    payload → the dict literal.
    let entries = PyList::empty(py);
    for (pos0, cand) in ranked.iter().enumerate() {
        let position = pos0 + 1;
        let row = cand.getattr("row")?;
        let run_obj = row.getattr("run_id")?;
        let run: String = run_obj.extract()?;
        let fam_obj = fam_map.call_method1("get", (&run_obj, &run_obj))?;
        let listed = members_of.get(&run).ok_or_else(|| {
            PyValueError::new_err(
                "internal: ranked candidate missing from members_of (unreachable)",
            )
        })?;
        let mask_obj = row.getattr("mask_bits")?;
        let reasons: Bound<'py, PyAny> = match reason_provider {
            None => none.clone(),
            Some(provider) => provider.call1((&mask_obj,))?,
        };
        let margin_roi: Bound<'py, PyAny> = match margin_roi_provider {
            None => none.clone(),
            Some(provider) => provider.call1((cand,))?,
        };
        let (cascade, cpcv_rate) = cascade_payload(py, &run_obj, join_for_entries, join_state)?;
        let entry = PyDict::new(py);
        entry.set_item("rank", position)?;
        entry.set_item("run_id", &run_obj)?;
        let mask: u128 = mask_obj.extract()?;
        entry.set_item(
            "mask_hex",
            format!("{mask:0width$x}", width = MASK_HEX_WIDTH),
        )?;
        entry.set_item(
            "conditions",
            list_f.call1((ctx.cond_names_fn.call1((&mask_obj,))?,))?,
        )?;
        entry.set_item("instrument", row.getattr("instrument")?)?;
        entry.set_item("tf", row.getattr("tf")?)?;
        entry.set_item("direction", row.getattr("direction")?)?;
        let arm = PyDict::new(py);
        arm.set_item("sl_paisa", cand.getattr("sl_paisa")?)?;
        arm.set_item("tp_paisa", cand.getattr("tp_paisa")?)?;
        entry.set_item("arm", arm)?;
        let agg = cand.getattr("agg")?;
        entry.set_item(
            "metrics",
            metrics_payload(py, &row, &agg, &cpcv_rate, &str_f, &json_loads)?,
        )?;
        entry.set_item(
            "regime",
            regime_payload(py, &agg, &json_loads, &ctx.error_cls)?,
        )?;
        entry.set_item("vix_band", vix_band_payload(py, &agg)?)?;
        entry.set_item("family_id", fam_obj)?;
        entry.set_item("family_members", list_f.call1((listed,))?)?;
        entry.set_item(
            "reasons",
            if reasons.is_none() {
                reasons
            } else {
                dict_f.call1((reasons,))?
            },
        )?;
        entry.set_item(
            "margin_roi",
            if margin_roi.is_none() {
                margin_roi
            } else {
                dict_f.call1((margin_roi,))?
            },
        )?;
        let equity_ref = PyDict::new(py);
        equity_ref.set_item("tuple", cand.getattr("tuple_ref")?)?;
        equity_ref.set_item("sidecar", "equity")?;
        entry.set_item("equity_ref", equity_ref)?;
        entry.set_item("cascade", cascade)?;
        entries.append(entry)?;
    }

    // 8. Honesty header + root cascade meta + artifact + `top100.built`.
    let mut hash_set: BTreeSet<String> = BTreeSet::new();
    for item in data_snapshot_hashes.try_iter()? {
        hash_set.insert(item?.extract()?);
    }
    let source = PyDict::new(py);
    source.set_item("data_snapshot_hashes", PyList::new(py, hash_set.iter())?)?;
    source.set_item("stores", counts.getattr("stores")?)?;
    source.set_item("pool", cands.len())?;
    source.set_item("gate_passers", passers.len())?;
    source.set_item("excluded_no_aggs", counts.getattr("excluded_no_aggs")?)?;
    source.set_item(
        "zero_trade_excluded",
        counts.getattr("zero_trade_excluded")?,
    )?;
    source.set_item("parity_excluded", counts.getattr("parity_excluded")?)?;
    source.set_item("families_collapsed", families_collapsed)?;
    source.set_item("reasons_wired", reason_provider.is_some())?;
    source.set_item("margin_roi_wired", margin_roi_provider.is_some())?;

    let cascade_meta = PyDict::new(py);
    if join_state == STATE_JOINED && join_for_entries.is_some() {
        let join = join_for_entries.ok_or_else(|| {
            PyValueError::new_err("internal: joined state without a join (unreachable)")
        })?;
        cascade_meta.set_item("state", join_state)?;
        cascade_meta.set_item("sweep_id", join.getattr("sweep_id")?)?;
        cascade_meta.set_item("n_input", join.getattr("n_input")?)?;
        cascade_meta.set_item("n_survived", join.getattr("n_survived")?)?;
    } else {
        cascade_meta.set_item("state", join_state)?;
        cascade_meta.set_item(
            "sweep_id",
            match cascade_join {
                None => none.clone(),
                Some(join) => join.getattr("sweep_id")?,
            },
        )?;
        cascade_meta.set_item("n_input", &none)?;
        cascade_meta.set_item("n_survived", &none)?;
    }

    let artifact_kwargs = PyDict::new(py);
    artifact_kwargs.set_item("schema_version", ctx.schema_version)?;
    artifact_kwargs.set_item("run_id", run_id)?;
    artifact_kwargs.set_item("preset", &resolved)?;
    artifact_kwargs.set_item("source", &source)?;
    artifact_kwargs.set_item("baselines", dict_f.call1((baselines,))?)?;
    artifact_kwargs.set_item("entries", PyTuple::new(py, entries.iter())?)?;
    artifact_kwargs.set_item("cascade_join", &cascade_meta)?;
    let artifact = ctx.artifact_cls.call((), Some(&artifact_kwargs))?;

    let log_kwargs = PyDict::new(py);
    log_kwargs.set_item("run_id", run_id)?;
    log_kwargs.set_item("preset", &resolved)?;
    log_kwargs.set_item("pool", cands.len())?;
    log_kwargs.set_item("gate_passers", passers.len())?;
    log_kwargs.set_item("families_collapsed", families_collapsed)?;
    log_kwargs.set_item("entries", entries.len())?;
    log_kwargs.set_item("cascade_join_state", join_state)?;
    ctx.logger
        .call_method("info", ("top100.built",), Some(&log_kwargs))?;
    Ok(artifact)
}

// ── validating reader (the ladder over already-read bytes) ──────────────────

/// The stamp half of the `load_top100` ladder: `stamp_not_json` /
/// `stamp_bad_shape` / `stamp_wrong_stage` / `stamp_wrong_run_id` /
/// `stamp_bad_content_hash` / `stamp_bad_produced_at` — or the validated
/// `content_hash` string object.
pub fn load_stamp<'py>(
    py: Python<'py>,
    stamp_raw: &Bound<'py, PyAny>,
    run_id: &Bound<'py, PyAny>,
    stamp_keys: &Bound<'py, PyAny>,
    stage_token: &str,
) -> PyResult<(Option<&'static str>, Option<Bound<'py, PyAny>>)> {
    let json_loads = py.import("json")?.getattr("loads")?;
    let Some(stamp) = loads_or_none(py, &json_loads, stamp_raw)? else {
        return Ok((Some("stamp_not_json"), None));
    };
    let shape_ok = stamp.is_instance_of::<PyDict>() && {
        let keyset = py
            .import("builtins")?
            .getattr("set")?
            .call1((stamp.call_method0("keys")?,))?;
        !py_ne(&keyset, stamp_keys)?
    };
    if !shape_ok {
        return Ok((Some("stamp_bad_shape"), None));
    }
    let stage_expected = PyString::new(py, stage_token);
    if py_ne(&stamp.get_item("stage")?, stage_expected.as_any())? {
        return Ok((Some("stamp_wrong_stage"), None));
    }
    if py_ne(&stamp.get_item("run_id")?, run_id)? {
        return Ok((Some("stamp_wrong_run_id"), None));
    }
    let content_hash = stamp.get_item("content_hash")?;
    if !content_hash.is_instance_of::<PyString>() || content_hash.len()? != SHA256_HEX_LEN {
        return Ok((Some("stamp_bad_content_hash"), None));
    }
    if !stamp
        .get_item("produced_at_utc")?
        .is_instance_of::<PyString>()
    {
        return Ok((Some("stamp_bad_produced_at"), None));
    }
    Ok((None, Some(content_hash)))
}

/// The artifact half of the ladder — hash gate FIRST (consumers VALIDATE the
/// hash before trusting the payload), then the B5 shape walk. Success
/// returns `(preset, source, normalized_baselines, entries, cascade_join)`;
/// the two bool==int PUNS legally load via Python `!=`.
pub fn load_artifact<'py>(
    py: Python<'py>,
    raw: &Bound<'py, PyAny>,
    content_hash: &Bound<'py, PyAny>,
    run_id: &Bound<'py, PyAny>,
    schema_version: i64,
    entry_required_keys: &Bound<'py, PyAny>,
) -> PyResult<(Option<&'static str>, Option<Bound<'py, PyTuple>>)> {
    let hexdigest = py
        .import("hashlib")?
        .getattr("sha256")?
        .call1((raw,))?
        .call_method0("hexdigest")?;
    if py_ne(&hexdigest, content_hash)? {
        return Ok((Some("artifact_hash_mismatch"), None));
    }
    let json_loads = py.import("json")?.getattr("loads")?;
    let Some(payload) = loads_or_none(py, &json_loads, raw)? else {
        return Ok((Some("artifact_not_json"), None));
    };
    if !payload.is_instance_of::<PyDict>() {
        return Ok((Some("artifact_not_object"), None));
    }
    let schema_obj = schema_version.into_pyobject(py)?;
    if py_ne(
        &payload.call_method1("get", ("schema_version",))?,
        schema_obj.as_any(),
    )? {
        return Ok((Some("schema_version_mismatch"), None));
    }
    if py_ne(&payload.call_method1("get", ("run_id",))?, run_id)? {
        return Ok((Some("artifact_wrong_run_id"), None));
    }
    let preset = payload.call_method1("get", ("preset",))?;
    if !preset.is_instance_of::<PyString>() || !preset.is_truthy()? {
        return Ok((Some("bad_preset"), None));
    }
    let source = payload.call_method1("get", ("source",))?;
    if !source.is_instance_of::<PyDict>() {
        return Ok((Some("bad_source"), None));
    }
    let baselines = payload.call_method1("get", ("baselines",))?;
    let baselines_ok = baselines.is_instance_of::<PyDict>() && {
        let mut all_str = true;
        for (key, value) in baselines.cast::<PyDict>()?.iter() {
            if !key.is_instance_of::<PyString>() || !value.is_instance_of::<PyString>() {
                all_str = false;
                break;
            }
        }
        all_str
    };
    if !baselines_ok {
        return Ok((Some("bad_baselines"), None));
    }
    let entries_raw = payload.call_method1("get", ("entries",))?;
    if !entries_raw.is_instance_of::<PyList>() {
        return Ok((Some("bad_entries"), None));
    }
    let entries = PyList::empty(py);
    for (pos0, item) in entries_raw.try_iter()?.enumerate() {
        let item = item?;
        let shape_ok = item.is_instance_of::<PyDict>()
            && entry_required_keys
                .call_method1("issubset", (item.call_method0("keys")?,))?
                .is_truthy()?;
        if !shape_ok {
            return Ok((Some("bad_entry_shape"), None));
        }
        let position = (pos0 + 1).into_pyobject(py)?;
        if py_ne(&item.get_item("rank")?, position.as_any())? {
            return Ok((Some("bad_entry_rank_order"), None));
        }
        entries.append(item)?;
    }
    // ADDITIVE root meta: presence-tolerant — a non-object value is dropped,
    // never a reject.
    let cascade_join_raw = payload.call_method1("get", ("cascade_join",))?;
    let cascade_join: Bound<'py, PyAny> = if cascade_join_raw.is_instance_of::<PyDict>() {
        cascade_join_raw
    } else {
        py.None().into_bound(py)
    };
    // `{str(key): str(value) …}` — CPython `str()` per the coercion pattern.
    let str_f = py.import("builtins")?.getattr("str")?;
    let normalized = PyDict::new(py);
    for (key, value) in baselines.cast::<PyDict>()?.iter() {
        normalized.set_item(str_f.call1((key,))?, str_f.call1((value,))?)?;
    }
    let pieces = PyTuple::new(
        py,
        [
            preset,
            source,
            normalized.into_any(),
            entries.into_any(),
            cascade_join,
        ],
    )?;
    Ok((None, Some(pieces)))
}
