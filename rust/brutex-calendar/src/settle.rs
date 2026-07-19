//! `brutex/calendars/settle_evidence.py::validate_evidence_payload` — the
//! byte-parity Rust twin (DEC-RUST-CALENDAR-LEAVES-001).
//!
//! Every refusal reproduces the Python `ValueError` text EXACTLY:
//! `settle evidence {path!r}: {reason}` — the caller passes the
//! already-repr'd path (`path_repr`), Rust renders the reason with the
//! [`crate::pyrepr`] twins. Rule ORDER is the Python source order
//! token-for-token; the per_day walk iterates in DOCUMENT order
//! (serde_json `preserve_order`), so the FIRST failing key decides the
//! message exactly as a Python dict walk would.

use serde_json::{Map, Value};
use std::cmp::Ordering;

use crate::pyiso::{IsoVerdict, datetime_fromisoformat_verdict, strptime_ymd_is_real};
use crate::pyrepr::{
    json_type_name, number_token_is_int, py_int_str, py_json_repr, py_str_repr, py_timedelta_str,
};

/// `SETTLE_EVIDENCE_SCHEMA` — exact int match.
pub const SETTLE_EVIDENCE_SCHEMA: &str = "1";
/// `SETTLE_EVIDENCE_KIND`.
pub const SETTLE_EVIDENCE_KIND: &str = "groww-postclose-settle-evidence";
/// `MIN_EVIDENCE_DAYS`.
pub const MIN_EVIDENCE_DAYS: i64 = 5;
/// `MIN_MARGIN_MINUTES`.
pub const MIN_MARGIN_MINUTES: i64 = 10;
/// `MAX_MARGIN_MINUTES`.
pub const MAX_MARGIN_MINUTES: i64 = 240;
/// `calendars.market_hours.BLOCK_END` = `time(15, 35)` in minutes-of-day.
pub const BLOCK_END_MINUTES: i64 = 15 * 60 + 35;

/// The valid outcome — the two rendered fields the Python side cannot
/// trivially re-derive plus nothing else (margin/days/version/earliest are
/// re-extracted Python-side from its own parse of the SAME document, under
/// the type guarantees this validation just proved).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SettleValid {
    /// `f"{adopted:%H:%M}"` — identical to the raw validated `HH:MM` string.
    pub adopted_hhmm: String,
    /// The raw validated `generated_at_utc` string.
    pub generated_raw: String,
}

type Fail = String;

fn fail(path_repr: &str, reason: String) -> Fail {
    format!("settle evidence {path_repr}: {reason}")
}

/// The int token of a value iff it is a Python `int` (bool refused, float
/// refused) — `None` otherwise.
fn int_token(v: &Value) -> Option<String> {
    match v {
        Value::Number(n) => {
            let t = n.to_string();
            if number_token_is_int(&t) {
                Some(t)
            } else {
                None
            }
        }
        _ => None,
    }
}

/// Compare a canonical (possibly huge) JSON int token against an i64 bound
/// with full precision.
fn cmp_int_token(token: &str, bound: i64) -> Ordering {
    let neg = token.starts_with('-');
    let digits = if neg { &token[1..] } else { token };
    if digits.len() <= 18 {
        let v: i64 = token.parse().expect("<=18-digit int token fits i64");
        v.cmp(&bound)
    } else if neg {
        Ordering::Less
    } else {
        Ordering::Greater
    }
}

/// `_require_int` — bool refused FIRST, then the int gate, then min, then
/// max. Returns the canonical token (Python `str(int)` — `-0` folds to `0`).
fn require_int(
    value: Option<&Value>,
    path_repr: &str,
    field: &str,
    minimum: Option<i64>,
    maximum: Option<i64>,
) -> Result<String, Fail> {
    let token = match value {
        Some(v) if !matches!(v, Value::Bool(_)) => int_token(v),
        _ => None,
    };
    let Some(token) = token else {
        return Err(fail(
            path_repr,
            format!(
                "{field} must be an int (bool refused), got {}",
                py_json_repr(value)
            ),
        ));
    };
    let canonical = py_int_str(&token);
    if let Some(min) = minimum
        && cmp_int_token(&canonical, min) == Ordering::Less
    {
        return Err(fail(
            path_repr,
            format!("{field}={canonical} is below the minimum {min}"),
        ));
    }
    if let Some(max) = maximum
        && cmp_int_token(&canonical, max) == Ordering::Greater
    {
        return Err(fail(
            path_repr,
            format!("{field}={canonical} is above the maximum {max}"),
        ));
    }
    Ok(canonical)
}

/// Strict ASCII `HH:MM` shape (`_HHMM_SHAPE.fullmatch` — exactly two ASCII
/// digits, a colon, two ASCII digits; a trailing newline fails).
fn hhmm_shape(s: &str) -> Option<(i64, i64)> {
    let b = s.as_bytes();
    if b.len() != 5 || b[2] != b':' {
        return None;
    }
    if !b[0].is_ascii_digit()
        || !b[1].is_ascii_digit()
        || !b[3].is_ascii_digit()
        || !b[4].is_ascii_digit()
    {
        return None;
    }
    let h = i64::from(b[0] - b'0') * 10 + i64::from(b[1] - b'0');
    let m = i64::from(b[3] - b'0') * 10 + i64::from(b[4] - b'0');
    Some((h, m))
}

/// `_require_hhmm` → the raw string + minutes-of-day.
fn require_hhmm<'v>(
    value: Option<&'v Value>,
    path_repr: &str,
    field: &str,
) -> Result<(&'v str, i64), Fail> {
    let Some(Value::String(raw)) = value else {
        return Err(fail(
            path_repr,
            format!(
                "{field} must be a string 'HH:MM', got {}",
                py_json_repr(value)
            ),
        ));
    };
    let Some((h, m)) = hhmm_shape(raw) else {
        return Err(fail(
            path_repr,
            format!("{field}={} is not strict ASCII HH:MM", py_str_repr(raw)),
        ));
    };
    if h > 23 || m > 59 {
        return Err(fail(
            path_repr,
            format!("{field}={} is not a real wall-clock time", py_str_repr(raw)),
        ));
    }
    Ok((raw, h * 60 + m))
}

/// `_require_str_list` — a list whose every item is a str.
fn require_str_list(value: Option<&Value>, path_repr: &str, field: &str) -> Result<(), Fail> {
    let ok = matches!(value, Some(Value::Array(items))
        if items.iter().all(|item| matches!(item, Value::String(_))));
    if ok {
        Ok(())
    } else {
        Err(fail(
            path_repr,
            format!(
                "{field} must be a list of strings, got {}",
                py_json_repr(value)
            ),
        ))
    }
}

/// `_validate_per_day` — the day → slot → cell matrix, walked in DOCUMENT
/// order. JSON object keys are always strings, so the Python
/// `isinstance(day, str)` / `isinstance(slot, str)` halves are vacuous here.
fn validate_per_day<'v>(
    value: Option<&'v Value>,
    path_repr: &str,
) -> Result<&'v Map<String, Value>, Fail> {
    let per_day = match value {
        Some(Value::Object(m)) => m,
        other => {
            let type_name = match other {
                None => "NoneType",
                Some(v) => json_type_name(v),
            };
            return Err(fail(
                path_repr,
                format!("per_day must be an object, got {type_name}"),
            ));
        }
    };
    if per_day.is_empty() {
        return Err(fail(
            path_repr,
            "per_day is empty — no observations".to_owned(),
        ));
    }
    for (day, slots) in per_day {
        let day_shape_ok = {
            let b = day.as_bytes();
            b.len() == 10
                && b[0..4].iter().all(u8::is_ascii_digit)
                && b[4] == b'-'
                && b[5..7].iter().all(u8::is_ascii_digit)
                && b[7] == b'-'
                && b[8..10].iter().all(u8::is_ascii_digit)
        };
        if !day_shape_ok {
            return Err(fail(
                path_repr,
                format!(
                    "per_day key {} is not an ISO calendar day",
                    py_str_repr(day)
                ),
            ));
        }
        if !strptime_ymd_is_real(day) {
            return Err(fail(
                path_repr,
                format!(
                    "per_day key {} is not a real calendar day",
                    py_str_repr(day)
                ),
            ));
        }
        let day_r = py_str_repr(day);
        let slots = match slots {
            Value::Object(m) if !m.is_empty() => m,
            _ => {
                return Err(fail(
                    path_repr,
                    format!("per_day[{day_r}] must be a non-empty object of slots"),
                ));
            }
        };
        for (slot, cell) in slots {
            // Slot keys are SHAPE-checked only — "99:99" passes (pinned).
            if hhmm_shape(slot).is_none() {
                return Err(fail(
                    path_repr,
                    format!(
                        "per_day[{day_r}] slot key {} is not HH:MM",
                        py_str_repr(slot)
                    ),
                ));
            }
            let slot_r = py_str_repr(slot);
            let Value::Object(cell) = cell else {
                return Err(fail(
                    path_repr,
                    format!("per_day[{day_r}][{slot_r}] must be an object"),
                ));
            };
            require_int(
                cell.get("records"),
                path_repr,
                &format!("per_day[{day_r}][{slot_r}].records"),
                Some(1),
                None,
            )?;
            if !matches!(cell.get("all_pass"), Some(Value::Bool(_))) {
                return Err(fail(
                    path_repr,
                    format!("per_day[{day_r}][{slot_r}].all_pass must be a bool"),
                ));
            }
            require_str_list(
                cell.get("failed_checks"),
                path_repr,
                &format!("per_day[{day_r}][{slot_r}].failed_checks"),
            )?;
        }
    }
    Ok(per_day)
}

/// The pure truth-table validator — `Err(message)` carries the EXACT Python
/// `ValueError` text; `Ok` carries [`SettleValid`].
pub fn validate_evidence_payload(payload: &Value, path_repr: &str) -> Result<SettleValid, Fail> {
    let Value::Object(map) = payload else {
        return Err(fail(
            path_repr,
            format!(
                "root is not a JSON object (got {})",
                json_type_name(payload)
            ),
        ));
    };
    // schema: bool refused FIRST (True == 1), then the exact-int gate.
    let schema = map.get("schema");
    let schema_ok = match schema {
        Some(Value::Bool(_)) | None => false,
        Some(v) => int_token(v).is_some_and(|t| t == SETTLE_EVIDENCE_SCHEMA),
    };
    if !schema_ok {
        return Err(fail(
            path_repr,
            format!(
                "schema must be exactly {SETTLE_EVIDENCE_SCHEMA}, got {}",
                py_json_repr(schema)
            ),
        ));
    }
    // kind: plain `!=` against the literal (any non-matching type unequal).
    let kind = map.get("kind");
    let kind_ok = matches!(kind, Some(Value::String(s)) if s == SETTLE_EVIDENCE_KIND);
    if !kind_ok {
        return Err(fail(
            path_repr,
            format!(
                "kind must be {}, got {}",
                py_str_repr(SETTLE_EVIDENCE_KIND),
                py_json_repr(kind)
            ),
        ));
    }
    let (earliest_raw, earliest_minutes) = require_hhmm(
        map.get("earliest_all_pass_ist"),
        path_repr,
        "earliest_all_pass_ist",
    )?;
    let (adopted_raw, adopted_minutes) = require_hhmm(
        map.get("adopted_settle_ist"),
        path_repr,
        "adopted_settle_ist",
    )?;
    let margin_tok = require_int(
        map.get("margin_minutes"),
        path_repr,
        "margin_minutes",
        Some(MIN_MARGIN_MINUTES),
        Some(MAX_MARGIN_MINUTES),
    )?;
    let margin: i64 = margin_tok
        .parse()
        .expect("validated margin is within 10..=240");
    if earliest_minutes + margin >= 24 * 60 {
        return Err(fail(
            path_repr,
            format!(
                "earliest_all_pass_ist + margin_minutes wraps past midnight ({} + {margin} min)",
                py_str_repr(earliest_raw)
            ),
        ));
    }
    if earliest_minutes + margin != adopted_minutes {
        return Err(fail(
            path_repr,
            format!(
                "adopted_settle_ist must equal earliest_all_pass_ist + margin_minutes \
                 ({} + {margin} min != {})",
                py_str_repr(earliest_raw),
                py_json_repr(map.get("adopted_settle_ist"))
            ),
        ));
    }
    if adopted_minutes < BLOCK_END_MINUTES {
        return Err(fail(
            path_repr,
            format!(
                "adopted_settle_ist {} is before the market-hours guard's BLOCK_END 15:35 IST \
                 (15:35-exact is allowed — the env-tier floor mirrored)",
                py_json_repr(map.get("adopted_settle_ist"))
            ),
        ));
    }
    let days_tok = require_int(
        map.get("distinct_trading_days"),
        path_repr,
        "distinct_trading_days",
        Some(MIN_EVIDENCE_DAYS),
        None,
    )?;
    let per_day = validate_per_day(map.get("per_day"), path_repr)?;
    // Support law: `earliest_key in slots and slots[earliest_key]
    // .get("all_pass") is True` — every cell was validated a dict with an
    // exact-bool all_pass above, so the object-and-true probe is exact.
    let support = per_day
        .values()
        .filter(|slots| {
            let Value::Object(m) = slots else {
                return false;
            };
            match m.get(earliest_raw) {
                Some(Value::Object(cell)) => {
                    matches!(cell.get("all_pass"), Some(Value::Bool(true)))
                }
                _ => false,
            }
        })
        .count();
    if days_tok != support.to_string() {
        return Err(fail(
            path_repr,
            format!(
                "per_day support for the earliest all-pass slot {} is {support} day(s) but \
                 distinct_trading_days claims {days_tok}",
                py_str_repr(earliest_raw)
            ),
        ));
    }
    let generated = map.get("generated_at_utc");
    let Some(Value::String(generated_raw)) = generated else {
        return Err(fail(
            path_repr,
            format!(
                "generated_at_utc must be an ISO-8601 string, got {}",
                py_json_repr(generated)
            ),
        ));
    };
    match datetime_fromisoformat_verdict(generated_raw) {
        IsoVerdict::Invalid => {
            return Err(fail(
                path_repr,
                format!(
                    "generated_at_utc={} is not ISO-8601",
                    py_str_repr(generated_raw)
                ),
            ));
        }
        IsoVerdict::Naive => {
            return Err(fail(
                path_repr,
                format!(
                    "generated_at_utc={} is tz-naive (must be tz-aware)",
                    py_str_repr(generated_raw)
                ),
            ));
        }
        IsoVerdict::Offset { total_us } => {
            return Err(fail(
                path_repr,
                format!(
                    "generated_at_utc={} must be UTC (got offset {})",
                    py_str_repr(generated_raw),
                    py_timedelta_str(total_us)
                ),
            ));
        }
        IsoVerdict::Utc => {}
    }
    let version = map.get("probe_script_version");
    let version_ok = matches!(version, Some(Value::String(s)) if !s.is_empty());
    if !version_ok {
        return Err(fail(
            path_repr,
            format!(
                "probe_script_version must be a non-empty string, got {}",
                py_json_repr(version)
            ),
        ));
    }
    for field in ["checks", "slots_observed", "transcripts"] {
        if map.contains_key(field) {
            require_str_list(map.get(field), path_repr, field)?;
        }
    }
    if let Some(excluded) = map.get("excluded_days") {
        let ok = matches!(excluded, Value::Object(m)
            if m.values().all(|v| matches!(v, Value::String(_))));
        if !ok {
            return Err(fail(
                path_repr,
                format!(
                    "excluded_days must be an object of strings, got {}",
                    py_json_repr(Some(excluded))
                ),
            ));
        }
    }
    if map.contains_key("ignored_records") {
        require_int(
            map.get("ignored_records"),
            path_repr,
            "ignored_records",
            Some(0),
            None,
        )?;
    }
    Ok(SettleValid {
        adopted_hhmm: adopted_raw.to_owned(),
        generated_raw: generated_raw.clone(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const PATH: &str = "'/tmp/x.json'";

    fn v(json: &str) -> Value {
        serde_json::from_str(json).expect("test payload is valid JSON")
    }

    fn base_json() -> String {
        let days = [
            "2026-07-06",
            "2026-07-07",
            "2026-07-08",
            "2026-07-09",
            "2026-07-10",
        ];
        let per_day: Vec<String> = days
            .iter()
            .map(|d| {
                format!(
                    "\"{d}\": {{\"15:45\": {{\"records\": 14, \"all_pass\": true, \
                     \"failed_checks\": []}}}}"
                )
            })
            .collect();
        format!(
            "{{\"schema\": 1, \"kind\": \"groww-postclose-settle-evidence\", \
             \"earliest_all_pass_ist\": \"15:45\", \"adopted_settle_ist\": \"16:00\", \
             \"margin_minutes\": 15, \"distinct_trading_days\": 5, \
             \"per_day\": {{{}}}, \
             \"generated_at_utc\": \"2026-07-11T05:00:00+00:00\", \
             \"probe_script_version\": \"postclose-settle-probe/1\"}}",
            per_day.join(", ")
        )
    }

    fn mutate(field: &str, replacement: &str) -> Value {
        let base = base_json();
        let start = base.find(&format!("\"{field}\"")).expect("field present");
        // Replace the whole `"field": <old-scalar>` clause (scalar fields only).
        let colon = base[start..].find(':').expect("colon") + start;
        let rest = &base[colon + 1..];
        let end_rel = rest
            .char_indices()
            .find(|(i, c)| {
                (*c == ',' || *c == '}') && {
                    // scalar clauses in base_json never nest at top level
                    let before = &rest[..*i];
                    before.matches('{').count() == before.matches('}').count()
                        && before.matches('[').count() == before.matches(']').count()
                }
            })
            .map(|(i, _)| i)
            .expect("clause end");
        let mut out = String::new();
        out.push_str(&base[..colon + 1]);
        out.push(' ');
        out.push_str(replacement);
        out.push_str(&rest[end_rel..]);
        v(&out)
    }

    fn err_of(payload: &Value) -> String {
        validate_evidence_payload(payload, PATH).expect_err("payload must refuse")
    }

    #[test]
    fn valid_base_passes() {
        let out = validate_evidence_payload(&v(&base_json()), PATH).expect("valid");
        assert_eq!(out.adopted_hhmm, "16:00");
        assert_eq!(out.generated_raw, "2026-07-11T05:00:00+00:00");
    }

    #[test]
    fn schema_boundary_twins() {
        assert_eq!(
            err_of(&mutate("schema", "true")),
            "settle evidence '/tmp/x.json': schema must be exactly 1, got True"
        );
        assert_eq!(
            err_of(&mutate("schema", "1.0")),
            "settle evidence '/tmp/x.json': schema must be exactly 1, got 1.0"
        );
        assert!(validate_evidence_payload(&v(&base_json()), PATH).is_ok());
    }

    #[test]
    fn margin_bounds_9_10_240_241() {
        // 9 refuses at the min bound; 241 at the max; 10 and 240 pass the
        // int gate (and then trip/pass the later arithmetic laws).
        assert_eq!(
            err_of(&mutate("margin_minutes", "9")),
            "settle evidence '/tmp/x.json': margin_minutes=9 is below the minimum 10"
        );
        assert_eq!(
            err_of(&mutate("margin_minutes", "241")),
            "settle evidence '/tmp/x.json': margin_minutes=241 is above the maximum 240"
        );
        // margin 10 with adopted fixed at 16:00 trips the ARITHMETIC law,
        // proving the bound itself passed.
        assert!(err_of(&mutate("margin_minutes", "10")).contains("must equal"));
        assert!(err_of(&mutate("margin_minutes", "240")).contains("must equal"));
    }

    #[test]
    fn huge_int_payloads_do_not_panic_and_render_full_digits() {
        let huge = "1000000000000000000000000000000";
        assert_eq!(
            err_of(&mutate("margin_minutes", huge)),
            format!(
                "settle evidence '/tmp/x.json': margin_minutes={huge} is above the maximum 240"
            )
        );
        assert_eq!(
            err_of(&mutate("margin_minutes", &format!("-{huge}"))),
            format!(
                "settle evidence '/tmp/x.json': margin_minutes=-{huge} is below the minimum 10"
            )
        );
        // A huge distinct_trading_days passes the min gate and fails the
        // support law with the full digits.
        let msg = err_of(&mutate("distinct_trading_days", huge));
        assert!(
            msg.ends_with(&format!("distinct_trading_days claims {huge}")),
            "{msg}"
        );
    }

    #[test]
    fn hhmm_trailing_newline_refused() {
        assert_eq!(
            err_of(&mutate("earliest_all_pass_ist", "\"15:45\\n\"")),
            "settle evidence '/tmp/x.json': earliest_all_pass_ist='15:45\\n' is not strict \
             ASCII HH:MM"
        );
    }

    #[test]
    fn wrap_boundary_1439_vs_1440() {
        // 23:49 + 10 = 1439 — no wrap; adopted 23:59 ≥ 15:35 — VALID.
        let mut p = mutate("earliest_all_pass_ist", "\"23:49\"");
        {
            let obj = p.as_object_mut().expect("object");
            obj.insert("margin_minutes".into(), v("10"));
            obj.insert("adopted_settle_ist".into(), v("\"23:59\""));
            let per_day = obj
                .get_mut("per_day")
                .and_then(Value::as_object_mut)
                .expect("per_day");
            let days: Vec<String> = per_day.keys().cloned().collect();
            for d in days {
                per_day.insert(
                    d,
                    v("{\"23:49\": {\"records\": 1, \"all_pass\": true, \"failed_checks\": []}}"),
                );
            }
        }
        assert!(validate_evidence_payload(&p, PATH).is_ok());
        // 23:50 + 10 = 1440 — wraps (>= is the law: 24:00-exact refused).
        let mut q = p.clone();
        {
            let obj = q.as_object_mut().expect("object");
            obj.insert("earliest_all_pass_ist".into(), v("\"23:50\""));
        }
        assert_eq!(
            err_of(&q),
            "settle evidence '/tmp/x.json': earliest_all_pass_ist + margin_minutes wraps past \
             midnight ('23:50' + 10 min)"
        );
    }

    #[test]
    fn floor_boundary_1534_vs_1535() {
        let mk = |earliest: &str, margin: &str, adopted: &str| {
            let mut p = mutate("earliest_all_pass_ist", &format!("\"{earliest}\""));
            let obj = p.as_object_mut().expect("object");
            obj.insert("margin_minutes".into(), v(margin));
            obj.insert("adopted_settle_ist".into(), v(&format!("\"{adopted}\"")));
            let per_day = obj
                .get_mut("per_day")
                .and_then(Value::as_object_mut)
                .expect("per_day");
            let days: Vec<String> = per_day.keys().cloned().collect();
            for d in days {
                per_day.insert(
                    d,
                    v(&format!(
                        "{{\"{earliest}\": {{\"records\": 1, \"all_pass\": true, \
                         \"failed_checks\": []}}}}"
                    )),
                );
            }
            p
        };
        assert_eq!(
            err_of(&mk("15:20", "14", "15:34")),
            "settle evidence '/tmp/x.json': adopted_settle_ist '15:34' is before the \
             market-hours guard's BLOCK_END 15:35 IST (15:35-exact is allowed — the env-tier \
             floor mirrored)"
        );
        assert!(validate_evidence_payload(&mk("15:20", "15", "15:35"), PATH).is_ok());
    }

    #[test]
    fn support_off_by_one_both_directions() {
        // days=6 with 5 supporting days.
        let msg = err_of(&mutate("distinct_trading_days", "6"));
        assert_eq!(
            msg,
            "settle evidence '/tmp/x.json': per_day support for the earliest all-pass slot \
             '15:45' is 5 day(s) but distinct_trading_days claims 6"
        );
        // 6 supporting days with days=5.
        let mut p = v(&base_json());
        p.as_object_mut()
            .expect("object")
            .get_mut("per_day")
            .and_then(Value::as_object_mut)
            .expect("per_day")
            .insert(
                "2026-07-11".into(),
                v("{\"15:45\": {\"records\": 1, \"all_pass\": true, \"failed_checks\": []}}"),
            );
        assert_eq!(
            err_of(&p),
            "settle evidence '/tmp/x.json': per_day support for the earliest all-pass slot \
             '15:45' is 6 day(s) but distinct_trading_days claims 5"
        );
    }

    #[test]
    fn generated_tz_matrix() {
        assert!(
            validate_evidence_payload(
                &mutate("generated_at_utc", "\"2026-07-11T05:00:00Z\""),
                PATH
            )
            .is_ok()
        );
        assert_eq!(
            err_of(&mutate("generated_at_utc", "\"2026-07-11T05:00:00+05:30\"")),
            "settle evidence '/tmp/x.json': generated_at_utc='2026-07-11T05:00:00+05:30' must \
             be UTC (got offset 5:30:00)"
        );
        assert_eq!(
            err_of(&mutate("generated_at_utc", "\"2026-07-11T05:00:00\"")),
            "settle evidence '/tmp/x.json': generated_at_utc='2026-07-11T05:00:00' is tz-naive \
             (must be tz-aware)"
        );
        assert_eq!(
            err_of(&mutate("generated_at_utc", "\"yesterday\"")),
            "settle evidence '/tmp/x.json': generated_at_utc='yesterday' is not ISO-8601"
        );
    }

    #[test]
    fn slot_key_9999_accepted_shape_only() {
        let mut p = v(&base_json());
        p.as_object_mut()
            .expect("object")
            .get_mut("per_day")
            .and_then(Value::as_object_mut)
            .expect("per_day")
            .insert(
                "2026-07-11".into(),
                v("{\"99:99\": {\"records\": 1, \"all_pass\": true, \"failed_checks\": []}}"),
            );
        // The extra day carries no 15:45 slot, so support stays 5 == days.
        assert!(validate_evidence_payload(&p, PATH).is_ok());
    }

    #[test]
    fn per_day_first_failing_key_decides_in_document_order() {
        // Two bad keys — the FIRST in document order names the message.
        let p = v(
            "{\"schema\": 1, \"kind\": \"groww-postclose-settle-evidence\", \
             \"earliest_all_pass_ist\": \"15:45\", \"adopted_settle_ist\": \"16:00\", \
             \"margin_minutes\": 15, \"distinct_trading_days\": 5, \
             \"per_day\": {\"bad-first\": {}, \"2026-02-30\": {}}, \
             \"generated_at_utc\": \"2026-07-11T05:00:00Z\", \
             \"probe_script_version\": \"x\"}",
        );
        assert_eq!(
            err_of(&p),
            "settle evidence '/tmp/x.json': per_day key 'bad-first' is not an ISO calendar day"
        );
    }
}
