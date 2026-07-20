//! CPython 3.12 datetime-parsing twins (DEC-RUST-CALENDAR-LEAVES-001).
//!
//! [`datetime_fromisoformat_verdict`] is a behavior-exact port of the
//! C-ACCELERATED `datetime.fromisoformat` (CPython 3.12
//! `Modules/_datetimemodule.c` — `_find_isoformat_datetime_separator` +
//! `parse_isoformat_date` + `parse_hh_mm_ss_ff` + `parse_isoformat_time` +
//! the constructor range checks), because the C module — NOT the pure-Python
//! `_pydatetime` — is what runs on the production interpreter, and the two
//! genuinely diverge (the C parser swallows one dangling separator-slot char
//! before a timezone, accepts `HHMMSSff` compact fractions, folds
//! out-of-range tz minutes, and treats a ±00:00:00.ffffff offset whose
//! whole-seconds part is zero as the UTC singleton). Every quirk here is
//! pinned by the probe-generated `tests/goldens/iso_goldens.json` battery.
//!
//! [`strptime_ymd_is_real`] mirrors the `datetime.strptime(day, "%Y-%m-%d")`
//! realness check on a shape-pre-gated `\d{4}-\d{2}-\d{2}` string, and
//! [`is_canonical_iso_date`] mirrors `date.fromisoformat(raw)` PLUS the
//! `raw == day.isoformat()` canonical round-trip (the observed-sessions
//! entry law — the round-trip rejects every non-`YYYY-MM-DD` spelling
//! `fromisoformat` would otherwise admit, so strict-shape + real-date is
//! exactly equivalent).
//!
//! One deliberate, safe divergence: the C sanitizer replaces a LONE
//! SURROGATE at a potential separator position with `T`. A Rust `&str`
//! cannot hold a lone surrogate, and serde_json refuses the `\udXXX` escape
//! that would produce one — the caller ABSTAINS to the Python oracle before
//! this module ever runs on such input.

/// The verdict the settle-evidence `generated_at_utc` law consumes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IsoVerdict {
    /// `ValueError` — "is not ISO-8601".
    Invalid,
    /// Parsed, `tzinfo is None` — "is tz-naive".
    Naive,
    /// Parsed, `utcoffset() == timedelta(0)` — passes the UTC-exact gate
    /// (includes the C quirk: a ±HH:MM:SS.ffffff offset with zero whole
    /// seconds is the UTC singleton, its microseconds DISCARDED).
    Utc,
    /// Parsed, aware, non-zero offset — "must be UTC (got offset ...)".
    Offset {
        /// The normalized offset in total microseconds (`str(timedelta)`
        /// renders the refusal message from this).
        total_us: i128,
    },
}

#[inline]
fn byte_at(b: &[u8], i: usize) -> u8 {
    // The C parser walks a NUL-terminated buffer whose logical end may be
    // interior (the tz boundary); reads at/after the true end see the
    // terminator. An EMBEDDED 0x00 byte inside the string behaves exactly
    // like that terminator (probe-pinned: "…Z\x00junk" validates as UTC).
    if i < b.len() { b[i] } else { 0 }
}

fn parse_digits(b: &[u8], pos: usize, count: usize) -> Option<(i32, usize)> {
    let mut v: i32 = 0;
    for i in 0..count {
        let c = byte_at(b, pos + i);
        if !c.is_ascii_digit() {
            return None;
        }
        v = v * 10 + i32::from(c - b'0');
    }
    Some((v, pos + count))
}

// ── proleptic-Gregorian ordinal math (the CPython datetime laws) ─────────

pub(crate) fn is_leap(y: i32) -> bool {
    y % 4 == 0 && (y % 100 != 0 || y % 400 == 0)
}

const DAYS_IN_MONTH: [i32; 13] = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

pub(crate) fn days_in_month(y: i32, m: i32) -> i32 {
    if m == 2 && is_leap(y) {
        29
    } else {
        DAYS_IN_MONTH[m as usize]
    }
}

fn days_before_year(y: i32) -> i64 {
    // C-truncating division on purpose (year 0 reaches this through the
    // week-date arm; C/Rust `/` agree, Python floor-divides — the C
    // accelerator governs).
    let y = i64::from(y - 1);
    y * 365 + y / 4 - y / 100 + y / 400
}

fn days_before_month(y: i32, m: i32) -> i32 {
    (1..m).map(|k| days_in_month(y, k)).sum()
}

fn ymd_to_ord(y: i32, m: i32, d: i32) -> i64 {
    days_before_year(y) + i64::from(days_before_month(y, m)) + i64::from(d)
}

const DI400Y: i64 = 146_097;
const DI100Y: i64 = 36_524;
const DI4Y: i64 = 1_461;

/// Ordinal (1 = 0001-01-01) → (year, month, day). Caller guarantees
/// `ord >= 1`; a resulting year outside 1..=9999 is range-checked upstream.
fn ord_to_ymd(ord: i64) -> (i32, i32, i32) {
    let mut n = ord - 1;
    let n400 = n.div_euclid(DI400Y);
    n = n.rem_euclid(DI400Y);
    let mut year = n400 * 400 + 1;
    let n100 = n / DI100Y;
    n %= DI100Y;
    let n4 = n / DI4Y;
    n %= DI4Y;
    let n1 = n / 365;
    n %= 365;
    year += n100 * 100 + n4 * 4 + n1;
    if n1 == 4 || n100 == 4 {
        let y = i32::try_from(year - 1).expect("ordinal years fit i32");
        return (y, 12, 31);
    }
    let y = i32::try_from(year).expect("ordinal years fit i32");
    let mut month = 1;
    let mut rem = i32::try_from(n).expect("day-of-year fits i32");
    loop {
        let dim = days_in_month(y, month);
        if rem < dim {
            return (y, month, rem + 1);
        }
        rem -= dim;
        month += 1;
    }
}

/// C `iso_week1_monday` — truncating `%` like C (negative first-days flow
/// through the year-0 week-date arm and end refused downstream).
fn iso_week1_monday(year: i32) -> i64 {
    const THURSDAY: i64 = 3;
    let first_day = ymd_to_ord(year, 1, 1);
    let first_weekday = (first_day + 6) % 7;
    let mut week1_monday = first_day - first_weekday;
    if first_weekday > THURSDAY {
        week1_monday += 7;
    }
    week1_monday
}

/// C `iso_to_ymd` — week 53 legal only for a year starting Thursday (or a
/// leap year starting Wednesday); day 1..=7; ordinal < 1 refused (the C
/// code would walk undefined territory there — every such input is an
/// eventual `ValueError` in Python).
fn iso_to_ymd(iso_year: i32, week: i32, day: i32) -> Option<(i32, i32, i32)> {
    if !(1..53).contains(&week) {
        let mut out_of_range = true;
        if week == 53 {
            let first_weekday = (ymd_to_ord(iso_year, 1, 1) + 6) % 7;
            if first_weekday == 3 || (first_weekday == 2 && is_leap(iso_year)) {
                out_of_range = false;
            }
        }
        if out_of_range {
            return None;
        }
    }
    if !(1..8).contains(&day) {
        return None;
    }
    let ord = iso_week1_monday(iso_year) + i64::from(week - 1) * 7 + i64::from(day - 1);
    if ord < 1 {
        return None;
    }
    Some(ord_to_ymd(ord))
}

// ── the C separator finder + date/time parsers ───────────────────────────

/// C `_find_isoformat_datetime_separator` (byte-level, len > 7 or == 7).
fn find_separator(b: &[u8]) -> Option<usize> {
    let n = b.len();
    if n == 7 {
        return Some(7);
    }
    if byte_at(b, 4) == b'-' {
        if byte_at(b, 5) == b'W' {
            if n < 8 {
                return None;
            }
            if n > 8 && byte_at(b, 8) == b'-' {
                if n == 9 {
                    return None;
                }
                if n > 10 && byte_at(b, 10).is_ascii_digit() {
                    // YYYY-Www-## ambiguity — assume the hyphen at 8.
                    return Some(8);
                }
                return Some(10);
            }
            Some(8)
        } else {
            Some(10)
        }
    } else if byte_at(b, 4) == b'W' {
        let mut idx = 7usize;
        while idx < n {
            if !b[idx].is_ascii_digit() {
                break;
            }
            idx += 1;
        }
        if idx < 9 {
            return Some(idx);
        }
        if idx.is_multiple_of(2) {
            Some(7)
        } else {
            Some(8)
        }
    } else {
        Some(8)
    }
}

/// C `parse_isoformat_date` over `[0, len)` (`len` = the separator location).
fn parse_iso_date(b: &[u8], len: usize) -> Option<(i32, i32, i32)> {
    let (year, mut p) = parse_digits(b, 0, 4)?;
    let uses_sep = byte_at(b, p) == b'-';
    if uses_sep {
        p += 1;
    }
    if byte_at(b, p) == b'W' {
        p += 1;
        let (week, np) = parse_digits(b, p, 2)?;
        p = np;
        let mut dayno = 1;
        if p < len {
            if uses_sep {
                if byte_at(b, p) != b'-' {
                    return None;
                }
                p += 1;
            }
            let (dn, _np) = parse_digits(b, p, 1)?;
            dayno = dn;
        }
        return iso_to_ymd(year, week, dayno);
    }
    let (month, np) = parse_digits(b, p, 2)?;
    p = np;
    if uses_sep {
        if byte_at(b, p) != b'-' {
            return None;
        }
        p += 1;
    }
    let (day, _np) = parse_digits(b, p, 2)?;
    Some((year, month, day))
}

struct Hms {
    h: i32,
    mi: i32,
    s: i32,
    us: i32,
    /// The C return value 1 — "ended on a consumed non-NUL char / has
    /// trailing content" (0 = ended exactly at a NUL byte).
    rv1: bool,
}

/// C `parse_hh_mm_ss_ff` over `[start, p_end)` — including the quirks the
/// goldens pin: one dangling char before `p_end` is consumed and forgiven
/// (`rv1` reports it), compact `HHMMSSff` fractions parse without a dot,
/// and an `i==2` `:` separator falls through into the fraction parser.
fn parse_hh_mm_ss_ff(b: &[u8], start: usize, p_end: usize) -> Option<Hms> {
    let mut vals = [0i32; 3];
    let mut p = start;
    let mut has_sep = true;
    let mut early: Option<bool> = None;
    for (i, slot) in vals.iter_mut().enumerate() {
        let (v, np) = parse_digits(b, p, 2)?;
        *slot = v;
        p = np;
        let c = byte_at(b, p);
        p += 1;
        if i == 0 {
            has_sep = c == b':';
        }
        if p >= p_end {
            early = Some(c != 0);
            break;
        } else if has_sep && c == b':' {
            continue;
        } else if c == b'.' || c == b',' {
            break;
        } else if !has_sep {
            p -= 1;
        } else {
            return None; // -4: malformed time separator
        }
    }
    if let Some(rv1) = early {
        return Some(Hms {
            h: vals[0],
            mi: vals[1],
            s: vals[2],
            us: 0,
            rv1,
        });
    }
    // Fraction parse — reached from BOTH the `.`/`,` break and the natural
    // loop end (the C fall-through that makes `12345678` read `.78`).
    let len_remains = p_end.saturating_sub(p);
    let to_parse = len_remains.min(6);
    let (mut us, np) = parse_digits(b, p, to_parse)?;
    p = np;
    const CORRECTION: [i32; 5] = [100_000, 10_000, 1_000, 100, 10];
    if (1..6).contains(&to_parse) {
        us *= CORRECTION[to_parse - 1];
    }
    while byte_at(b, p).is_ascii_digit() {
        p += 1; // skip truncated fraction digits
    }
    Some(Hms {
        h: vals[0],
        mi: vals[1],
        s: vals[2],
        us,
        rv1: byte_at(b, p) != 0,
    })
}

enum TzKind {
    Naive,
    /// `Z`, or a numeric offset whose whole-seconds part is zero (the C
    /// `tzoffset == 0 → UTC singleton` quirk — microseconds dropped).
    Utc,
    Offset {
        total_us: i128,
    },
}

struct TimeParse {
    h: i32,
    mi: i32,
    s: i32,
    tz: TzKind,
}

/// C `parse_isoformat_time` over `[t0, t_end)`.
fn parse_iso_time(b: &[u8], t0: usize, t_end: usize) -> Option<TimeParse> {
    // The tz scan — a do-while over the FIRST 'Z'/'+'/'-' occurrence.
    let mut tzpos = t0;
    loop {
        let c = byte_at(b, tzpos);
        if c == b'Z' || c == b'+' || c == b'-' {
            break;
        }
        tzpos += 1;
        if tzpos >= t_end {
            break;
        }
    }
    let hms = parse_hh_mm_ss_ff(b, t0, tzpos)?;
    if tzpos == t_end {
        // No tz — trailing content is an error (C rv==1 → -5).
        if hms.rv1 {
            return None;
        }
        return Some(TimeParse {
            h: hms.h,
            mi: hms.mi,
            s: hms.s,
            tz: TzKind::Naive,
        });
    }
    if byte_at(b, tzpos) == b'Z' {
        // 'Z' must sit at the (possibly embedded-NUL) string end.
        if byte_at(b, tzpos + 1) != 0 {
            return None;
        }
        return Some(TimeParse {
            h: hms.h,
            mi: hms.mi,
            s: hms.s,
            tz: TzKind::Utc,
        });
    }
    let sign: i128 = if byte_at(b, tzpos) == b'-' { -1 } else { 1 };
    let tz = parse_hh_mm_ss_ff(b, tzpos + 1, t_end)?;
    if tz.rv1 {
        return None; // C: `return rv ? -5 : 1`
    }
    // NO per-field range check (probe-pinned: `+05:75` folds to 6:15).
    let secs = i128::from(tz.h) * 3600 + i128::from(tz.mi) * 60 + i128::from(tz.s);
    let kind = if secs == 0 {
        TzKind::Utc // the C tzoffset==0 → UTC-singleton quirk (µs dropped)
    } else {
        TzKind::Offset {
            total_us: sign * (secs * 1_000_000 + i128::from(tz.us)),
        }
    };
    Some(TimeParse {
        h: hms.h,
        mi: hms.mi,
        s: hms.s,
        tz: kind,
    })
}

/// The full `datetime.fromisoformat` verdict (see [`IsoVerdict`]).
pub fn datetime_fromisoformat_verdict(s: &str) -> IsoVerdict {
    // C `_sanitize_isoformat_str`: length in CODE POINTS must be >= 7.
    if s.chars().count() < 7 {
        return IsoVerdict::Invalid;
    }
    let b = s.as_bytes();
    let n = b.len();
    let Some(sep_loc) = find_separator(b) else {
        return IsoVerdict::Invalid;
    };
    let Some((y, mo, d)) = parse_iso_date(b, sep_loc) else {
        return IsoVerdict::Invalid;
    };
    // `new_datetime` date range checks.
    if !(1..=9999).contains(&y) || !(1..=12).contains(&mo) || d < 1 || d > days_in_month(y, mo) {
        return IsoVerdict::Invalid;
    }
    if n <= sep_loc {
        return IsoVerdict::Naive; // date-only
    }
    // Skip the ONE separator char (UTF-8 lead-byte width, the C switch).
    let lead = b[sep_loc];
    let skip = if lead & 0x80 == 0 {
        1
    } else {
        match lead & 0xf0 {
            0xe0 => 3,
            0xf0 => 4,
            _ => 2,
        }
    };
    let t0 = sep_loc + skip;
    if t0 > n {
        // A truncated multi-byte tail — the C code would hand the time
        // parser a negative length (UB territory); refuse.
        return IsoVerdict::Invalid;
    }
    let Some(tp) = parse_iso_time(b, t0, n) else {
        return IsoVerdict::Invalid;
    };
    // `new_datetime` time range checks (µs bounded by the parse itself).
    if !(0..=23).contains(&tp.h) || !(0..=59).contains(&tp.mi) || !(0..=59).contains(&tp.s) {
        return IsoVerdict::Invalid;
    }
    match tp.tz {
        TzKind::Naive => IsoVerdict::Naive,
        TzKind::Utc => IsoVerdict::Utc,
        TzKind::Offset { total_us } => {
            // `timezone()` — strictly between ±24 h.
            if total_us.abs() >= 86_400_000_000 {
                IsoVerdict::Invalid
            } else {
                IsoVerdict::Offset { total_us }
            }
        }
    }
}

// ── strict-date twins for the two calendar laws ──────────────────────────

/// `datetime.strptime(day, "%Y-%m-%d")` realness on a SHAPE-PRE-GATED
/// `\d{4}-\d{2}-\d{2}` ASCII string (the settle per_day key law): a real
/// proleptic-Gregorian date with year 1..=9999.
pub fn strptime_ymd_is_real(day: &str) -> bool {
    let b = day.as_bytes();
    debug_assert!(b.len() == 10 && b[4] == b'-' && b[7] == b'-');
    let Some((y, _)) = parse_digits(b, 0, 4) else {
        return false;
    };
    let Some((m, _)) = parse_digits(b, 5, 2) else {
        return false;
    };
    let Some((d, _)) = parse_digits(b, 8, 2) else {
        return false;
    };
    (1..=9999).contains(&y) && (1..=12).contains(&m) && d >= 1 && d <= days_in_month(y, m)
}

/// The observed-sessions entry-date law: `date.fromisoformat(raw)` succeeds
/// AND `raw == day.isoformat()`. `isoformat()` always renders
/// `%04d-%02d-%02d`, so the round-trip holds IFF `raw` is exactly the strict
/// zero-padded canonical form of a real date — strict shape + realness is
/// provably equivalent (every other `fromisoformat`-accepted spelling —
/// compact `YYYYMMDD`, week dates — fails the round-trip).
pub fn is_canonical_iso_date(raw: &str) -> bool {
    let b = raw.as_bytes();
    if b.len() != 10 || b[4] != b'-' || b[7] != b'-' {
        return false;
    }
    if !b[0..4].iter().all(u8::is_ascii_digit)
        || !b[5..7].iter().all(u8::is_ascii_digit)
        || !b[8..10].iter().all(u8::is_ascii_digit)
    {
        return false;
    }
    strptime_ymd_is_real(raw)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_date_law() {
        assert!(is_canonical_iso_date("2026-05-13"));
        assert!(is_canonical_iso_date("2024-02-29")); // leap
        assert!(is_canonical_iso_date("0001-01-01"));
        assert!(is_canonical_iso_date("9999-12-31"));
        assert!(!is_canonical_iso_date("2023-02-29")); // non-leap
        assert!(!is_canonical_iso_date("20260513")); // compact — round-trip fails
        assert!(!is_canonical_iso_date("2026-5-13")); // unpadded
        assert!(!is_canonical_iso_date("2026-W20-3")); // week form
        assert!(!is_canonical_iso_date("0000-01-01")); // year 0
        assert!(!is_canonical_iso_date("2026-13-01"));
        assert!(!is_canonical_iso_date("2026-00-13"));
        assert!(!is_canonical_iso_date("2026-05-00"));
        assert!(!is_canonical_iso_date("2026-05-13 "));
        assert!(!is_canonical_iso_date("٢٠٢٦-05-13")); // unicode digits
    }

    #[test]
    fn strptime_realness_law() {
        assert!(strptime_ymd_is_real("2026-07-11"));
        assert!(!strptime_ymd_is_real("0000-01-01"));
        assert!(!strptime_ymd_is_real("2026-02-30"));
        assert!(!strptime_ymd_is_real("2026-00-11"));
        assert!(!strptime_ymd_is_real("2026-07-00"));
        assert!(strptime_ymd_is_real("2024-02-29"));
    }

    #[test]
    fn iso_verdict_spot_checks() {
        use IsoVerdict::*;
        assert_eq!(
            datetime_fromisoformat_verdict("2026-07-11T05:00:00+00:00"),
            Utc
        );
        assert_eq!(datetime_fromisoformat_verdict("2026-07-11T05:00:00Z"), Utc);
        assert_eq!(datetime_fromisoformat_verdict("2026-07-11T05:00:00"), Naive);
        assert_eq!(
            datetime_fromisoformat_verdict("2026-07-11T05:00:00+05:30"),
            Offset {
                total_us: 19_800_000_000
            }
        );
        assert_eq!(
            datetime_fromisoformat_verdict("2026-07-11T05:00:00-05:30"),
            Offset {
                total_us: -19_800_000_000
            }
        );
        // The C quirks (probe-pinned):
        assert_eq!(
            datetime_fromisoformat_verdict("2026-07-11T05:00:00+00:00:00.000001"),
            Utc
        );
        assert_eq!(
            datetime_fromisoformat_verdict("2026-07-11T05:00:00z"),
            Invalid
        );
        assert_eq!(datetime_fromisoformat_verdict("x"), Invalid);
    }
}
