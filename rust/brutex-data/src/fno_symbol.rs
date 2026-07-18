//! The F&O contract-symbol STRUCTURAL grammar — the ASCII-only Rust mirror
//! of `brutex/data/fno_symbol.py::_parse_fno_symbol_python` (the LIVE
//! Python parity oracle; DEC-DATA-RUST-PURELAW-001, MIGRATION.md #1940).
//!
//! ## Division of decisions (facade contract)
//!
//! Rust owns: token count/roles (right-anchored split), the type token
//! (case-SENSITIVE exact `FUT`/`CE`/`PE`), the expiry (`%d%b%y` format +
//! POSIX `%y` pivot + Gregorian calendar + the [2019, 2059] window),
//! exchange/underlying/strike-token non-emptiness. Python owns: strike
//! VALIDITY (`Decimal` semantics never cross — Rust reports only the
//! strike-token SPAN) + ALL object materialization.
//!
//! ## The `%d%b%y` mirror (EMPIRICAL — probed CPython 3.12, 2026-07-17)
//!
//! CPython `_strptime.TimeRE()` compiles (probed directly, C/POSIX
//! LC_TIME — the ONLY locale the facade's LOCALE GUARD ever engages under):
//!
//! ```text
//! %d → 3[0-1]|[1-2]\d|0[1-9]|[1-9]| [1-9]     # NOTE the space-digit arm!
//! %b → jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec   (case-insensitive)
//! %y → \d\d
//! ```
//!
//! matched with `.match()` + `found.end() == len(data_string)` (an
//! effective fullmatch), then `int()` on the captures, the POSIX pivot
//! (`00–68` → 2000+y, `69–99` → 1900+y), then `datetime(year, month, day)`
//! (real-calendar validation). The mirror tries the day alternatives IN
//! ORDER with full-remainder backtracking (on remainder failure fall
//! through to the next alternative — provably unambiguous here since
//! months are alphabetic, but the backtracking spec keeps the mirror
//! token-for-token rather than proof-dependent); a remainder SUCCESS
//! commits the day (calendar/window failure then rejects WITHOUT trying
//! further alternatives — exactly the regex-commit-then-validate order
//! CPython runs). The full probe truth table is transcribed into this
//! file's unit tests, so the mirror is empirically anchored on BOTH sides
//! (the Python golden corpus carries the same rows end-to-end).
//!
//! ASCII-only by contract: the facade's ASCII pre-gate routes any
//! non-ASCII symbol straight to the Python oracle (CPython's `\d` and
//! `int()` accept Unicode digits — reproducing Unicode Nd tables here
//! would be drift bait for zero product value). This module operates on
//! BYTES and never slices a `str`, so it cannot panic on arbitrary UTF-8
//! input either (proptest-pinned).

use crate::{EXPIRY_YEAR_MAX, EXPIRY_YEAR_MIN};

/// The English 3-letter month table (C/POSIX LC_TIME) — lowercased ASCII.
/// The facade's LOCALE GUARD refuses to engage unless the live Python
/// oracle's `_strptime.LocaleTime` month vocabulary equals exactly this
/// table, so the mirror is only ever consulted under the locale it mirrors.
const MONTHS: [&[u8; 3]; 12] = [
    b"jan", b"feb", b"mar", b"apr", b"may", b"jun", b"jul", b"aug", b"sep", b"oct", b"nov", b"dec",
];

/// The FUT type code (tuple slot 0 vocabulary; facade-shared).
const TYPE_FUT: u8 = 0;
/// The CE type code.
const TYPE_CE: u8 = 1;
/// The PE type code.
const TYPE_PE: u8 = 2;

/// Days in *month* of *year* under the Gregorian rule (leap: `y%4==0 &&
/// (y%100!=0 || y%400==0)`). `month` is 1-12 (caller-guaranteed).
fn days_in_month(month: u8, year: i32) -> u8 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        _ => {
            // month == 2 (the caller never passes 0 / >12 — month comes
            // from the 12-entry table index).
            if year % 4 == 0 && (year % 100 != 0 || year % 400 == 0) {
                29
            } else {
                28
            }
        }
    }
}

/// Match the month + year TAIL of an expiry token (`Sep25` — exactly 3
/// month chars + 2 ASCII digits, end-anchored). Returns `(month_1_12,
/// two_digit_year)` or `None`. Month compare is ASCII-lowercase vs the
/// English table (the probed case-insensitive `%b`).
fn parse_month_year(rest: &[u8]) -> Option<(u8, u8)> {
    if rest.len() != 5 {
        return None; // %y is EXACTLY 2 digits + fullmatch (probe: `30Sep2025` rejects)
    }
    let m0 = rest[0].to_ascii_lowercase();
    let m1 = rest[1].to_ascii_lowercase();
    let m2 = rest[2].to_ascii_lowercase();
    let month = MONTHS
        .iter()
        .position(|abbr| abbr[0] == m0 && abbr[1] == m1 && abbr[2] == m2)?;
    let y0 = rest[3];
    let y1 = rest[4];
    if !y0.is_ascii_digit() || !y1.is_ascii_digit() {
        return None;
    }
    Some(((month + 1) as u8, (y0 - b'0') * 10 + (y1 - b'0')))
}

/// The ordered `%d` day alternatives (the probed CPython regex, in ORDER):
/// `3[01]` → `[12]\d` → `0[1-9]` → `[1-9]` → `' '[1-9]`. Each yields
/// `(day, consumed_prefix_len)`; the caller tries them in order with
/// full-remainder backtracking.
fn day_alternatives(token: &[u8]) -> [Option<(u8, usize)>; 5] {
    let b0 = token.first().copied();
    let b1 = token.get(1).copied();
    [
        // 3[01]
        match (b0, b1) {
            (Some(b'3'), Some(d @ (b'0' | b'1'))) => Some((30 + (d - b'0'), 2)),
            _ => None,
        },
        // [12]\d
        match (b0, b1) {
            (Some(t @ (b'1' | b'2')), Some(d)) if d.is_ascii_digit() => {
                Some(((t - b'0') * 10 + (d - b'0'), 2))
            }
            _ => None,
        },
        // 0[1-9]
        match (b0, b1) {
            (Some(b'0'), Some(d @ b'1'..=b'9')) => Some((d - b'0', 2)),
            _ => None,
        },
        // [1-9]
        match b0 {
            Some(d @ b'1'..=b'9') => Some((d - b'0', 1)),
            _ => None,
        },
        // ' [1-9]' — the space-digit arm (single-digit ONLY; probe:
        // `' 3Sep25'` accepts, `' 30Sep25'` rejects).
        match (b0, b1) {
            (Some(b' '), Some(d @ b'1'..=b'9')) => Some((d - b'0', 2)),
            _ => None,
        },
    ]
}

/// Parse a `DDMmmYY` expiry token — the ASCII mirror of
/// `parse_expiry_token` (strptime fullmatch + POSIX pivot + calendar +
/// the [`EXPIRY_YEAR_MIN`], [`EXPIRY_YEAR_MAX`] window). Returns
/// `(resolved_year, month, day)` or `None`.
fn parse_expiry_ascii(token: &[u8]) -> Option<(i32, u8, u8)> {
    for alt in day_alternatives(token) {
        let Some((day, consumed)) = alt else {
            continue;
        };
        let Some((month, yy)) = parse_month_year(&token[consumed..]) else {
            continue; // remainder failure — backtrack to the next alternative
        };
        // Remainder SUCCESS commits this day (the regex-commit-then-
        // validate order): calendar/window failure rejects WITHOUT
        // trying further alternatives.
        let year = if yy <= 68 {
            2000 + i32::from(yy)
        } else {
            1900 + i32::from(yy)
        };
        if day > days_in_month(month, year) {
            return None; // datetime() calendar validation (probe: 31Sep25 / 29Feb25)
        }
        if !(EXPIRY_YEAR_MIN..=EXPIRY_YEAR_MAX).contains(&year) {
            return None; // the DEC-FNO-EXPIRY-YEAR-PIVOT-001 window
        }
        return Some((year, month, day));
    }
    None
}

/// Decode an F&O contract symbol into the 9-slot integer coordinates, or
/// `None` — the structural mirror of the Python law (module docs).
///
/// Layout (spans are byte offsets into the ORIGINAL string — the facade
/// guarantees ASCII, so byte offsets ≡ char offsets):
///
/// ```text
/// (type_code,        # 0=FUT · 1=CE · 2=PE
///  exchange_end,     # exchange = symbol[0:exchange_end]
///  underlying_start, # underlying = symbol[underlying_start:underlying_end]
///  underlying_end,
///  expiry_year,      # RESOLVED 4-digit year (pivot applied, window-checked)
///  expiry_month,
///  expiry_day,
///  strike_start,     # option: strike token = symbol[strike_start:strike_end]
///  strike_end)       # future: (0, 0) — absent, facade-refuse-checked
/// ```
///
/// Never panics for any input (proptest-pinned; byte-based — no `str`
/// slicing). `None` mirrors every oracle rejection this structural half
/// owns: junk/spot symbols, short token counts, a bad expiry token, an
/// empty exchange / underlying / (option) strike token. A NON-empty junk
/// strike token is deliberately ACCEPTED here (span reported) — its
/// validity is Python's (`parse_strike_token`), and the facade returns
/// `None` when that oracle helper rejects, matching the Python law.
#[allow(clippy::type_complexity)]
pub fn parse_fno_symbol_coords(
    symbol: &str,
) -> Option<(u8, usize, usize, usize, i32, u8, u8, usize, usize)> {
    let bytes = symbol.as_bytes();
    // Token spans, split on b'-' — mirrors `symbol.split("-")` (an empty
    // string yields one empty token, exactly like Python).
    let mut tokens: Vec<(usize, usize)> = Vec::new();
    let mut start = 0usize;
    for (i, &b) in bytes.iter().enumerate() {
        if b == b'-' {
            tokens.push((start, i));
            start = i + 1;
        }
    }
    tokens.push((start, bytes.len()));

    let n = tokens.len();
    let (t_start, t_end) = tokens[n - 1];
    let type_code = match &bytes[t_start..t_end] {
        b"FUT" => TYPE_FUT,
        b"CE" => TYPE_CE,
        b"PE" => TYPE_PE,
        _ => return None, // case-SENSITIVE exact match (`fut`/`ce` reject)
    };

    let (expiry_span, strike_span, underlying_last) = if type_code == TYPE_FUT {
        if n < 4 {
            return None; // EXCH-SYMBOL-EXPIRY-FUT needs >= 4 tokens
        }
        (tokens[n - 2], (0usize, 0usize), tokens[n - 3])
    } else {
        if n < 5 {
            return None; // EXCH-SYMBOL-EXPIRY-STRIKE-TYPE needs >= 5 tokens
        }
        let strike = tokens[n - 2];
        if strike.0 >= strike.1 {
            // An EMPTY strike token (`NSE-NIFTY-30Sep25--CE`) is an oracle
            // `None` (`parse_strike_token("")` rejects) — structural here.
            return None;
        }
        (tokens[n - 3], strike, tokens[n - 4])
    };

    let (year, month, day) = parse_expiry_ascii(&bytes[expiry_span.0..expiry_span.1])?;

    let exchange_end = tokens[0].1;
    if exchange_end == 0 {
        return None; // empty exchange (`-NIFTY-…`) — oracle `None`
    }
    // underlying = "-".join(parts[1..last_underlying_token]) — a CONTIGUOUS
    // substring of the original symbol (the load-bearing span law).
    let underlying_start = tokens[1].0;
    let underlying_end = underlying_last.1;
    if underlying_start >= underlying_end {
        return None; // empty underlying (`NSE--30Sep25-FUT`) — oracle `None`
    }

    Some((
        type_code,
        exchange_end,
        underlying_start,
        underlying_end,
        year,
        month,
        day,
        strike_span.0,
        strike_span.1,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    /// Shorthand: parse + expect acceptance, returning the tuple.
    fn ok(symbol: &str) -> (u8, usize, usize, usize, i32, u8, u8, usize, usize) {
        parse_fno_symbol_coords(symbol)
            .unwrap_or_else(|| panic!("expected {symbol:?} to parse, got None"))
    }

    /// Shorthand: parse + expect rejection.
    fn none(symbol: &str) {
        assert!(
            parse_fno_symbol_coords(symbol).is_none(),
            "expected {symbol:?} to reject, got {:?}",
            parse_fno_symbol_coords(symbol)
        );
    }

    /// Expiry-only probe through a well-formed FUT scaffold.
    fn expiry(token: &str) -> Option<(i32, u8, u8)> {
        parse_expiry_ascii(token.as_bytes())
    }

    // ------------------------------------------------------------------
    // The probed strptime `%d%b%y` truth table (design §2.4 — every row).
    // ------------------------------------------------------------------

    #[test]
    fn month_case_insensitive_accepts() {
        for tok in ["30Sep25", "30SEP25", "30sep25", "30sEp25", "30SeP25"] {
            assert_eq!(expiry(tok), Some((2025, 9, 30)), "{tok}");
        }
    }

    #[test]
    fn single_digit_day_arm() {
        assert_eq!(expiry("3Sep25"), Some((2025, 9, 3)));
    }

    #[test]
    fn zero_padded_day_arm() {
        assert_eq!(expiry("03Sep25"), Some((2025, 9, 3)));
    }

    #[test]
    fn space_digit_day_arm_accepts_single_digit() {
        // The `' [1-9]'` arm — ASCII, probe-confirmed ACCEPT.
        assert_eq!(expiry(" 3Sep25"), Some((2025, 9, 3)));
    }

    #[test]
    fn space_two_digit_day_rejects() {
        // The killing twin: the space arm is `' [1-9]'`, single-digit ONLY —
        // a naive `' \d\d'` mirror would wrongly accept.
        assert_eq!(expiry(" 30Sep25"), None);
    }

    #[test]
    fn whitespace_fullmatch_rejects() {
        for tok in ["30Sep25 ", "30 Sep25", "30Sep 25", "30Sep25\n"] {
            assert_eq!(expiry(tok), None, "{tok:?}");
        }
    }

    #[test]
    fn day_grammar_rejects() {
        for tok in ["030Sep25", "00Sep25", "302Sep25"] {
            assert_eq!(expiry(tok), None, "{tok}");
        }
    }

    #[test]
    fn calendar_validation() {
        assert_eq!(expiry("31Sep25"), None); // Sep has 30 days
        assert_eq!(expiry("29Feb25"), None); // 2025 not a leap year
        assert_eq!(expiry("29Feb24"), Some((2024, 2, 29))); // 2024 leap
        assert_eq!(expiry("31Apr25"), None); // Apr has 30 days
        // 29Feb00: the %400 leap arm ACCEPTS the calendar (2000 IS leap)
        // but the [2019, 2059] window then rejects — None end-to-end. The
        // %100/%400 arms have no in-window year (2100 > 2059), so the leap
        // helper is pinned directly below.
        assert_eq!(expiry("29Feb00"), None);
        assert_eq!(days_in_month(2, 2000), 29); // %400 leap
        assert_eq!(days_in_month(2, 1900), 28); // %100 non-leap
        assert_eq!(days_in_month(2, 2024), 29); // %4 leap
        assert_eq!(days_in_month(2, 2025), 28);
    }

    #[test]
    fn month_table_rejects() {
        for tok in ["30Sept25", "30September25", "30Xxx25"] {
            assert_eq!(expiry(tok), None, "{tok}");
        }
    }

    #[test]
    fn year_exactly_two_digits() {
        assert_eq!(expiry("30Sep2025"), None);
        assert_eq!(expiry("30Sep5"), None);
    }

    #[test]
    fn posix_pivot_years() {
        // Underlying pivot law (the window then rejects the out-of-window
        // resolutions — pinned in the window tests below; here the pivot
        // itself is probed through in-window years + the boundary math).
        assert_eq!(expiry("30Sep25"), Some((2025, 9, 30))); // 25 → 2025
        assert_eq!(expiry("01Jan19"), Some((2019, 1, 1))); // 19 → 2019 (window floor)
        assert_eq!(expiry("31Dec59"), Some((2059, 12, 31))); // 59 → 2059 (window cap)
        assert_eq!(expiry("30Sep00"), None); // 00 → 2000, pre-window
    }

    #[test]
    fn window_rejects_out_of_range_years() {
        assert_eq!(expiry("31Dec18"), None); // 2018 < 2019
        assert_eq!(expiry("01Jan60"), None); // pivot 60 → 2060 > 2059
        assert_eq!(expiry("30Jun75"), None); // pivot 75 → 1975 (junk-year law)
        // The %y 68/69 pivot boundary: BOTH sides resolve OUTSIDE the
        // window (68 → 2068, 69 → 1969) — no in-window accept twin exists.
        assert_eq!(expiry("30Sep68"), None);
        assert_eq!(expiry("30Sep69"), None);
        assert_eq!(expiry("30Sep99"), None); // 1999
    }

    // ------------------------------------------------------------------
    // Grammar edges (design §4.1 mirror — the structural law).
    // ------------------------------------------------------------------

    #[test]
    fn well_formed_future() {
        let (t, xe, us, ue, y, m, d, ss, se) = ok("NSE-NIFTY-30Sep25-FUT");
        assert_eq!((t, xe, us, ue), (TYPE_FUT, 3, 4, 9));
        assert_eq!((y, m, d), (2025, 9, 30));
        assert_eq!((ss, se), (0, 0)); // FUT: strike span ABSENT
        assert_eq!(&"NSE-NIFTY-30Sep25-FUT"[0..xe], "NSE");
        assert_eq!(&"NSE-NIFTY-30Sep25-FUT"[us..ue], "NIFTY");
    }

    #[test]
    fn well_formed_option_reports_strike_span() {
        let sym = "NSE-NIFTY-30Sep25-24650-CE";
        let (t, xe, us, ue, y, m, d, ss, se) = ok(sym);
        assert_eq!(t, TYPE_CE);
        assert_eq!(&sym[0..xe], "NSE");
        assert_eq!(&sym[us..ue], "NIFTY");
        assert_eq!((y, m, d), (2025, 9, 30));
        assert_eq!(&sym[ss..se], "24650"); // TOKEN span only — Python parses it
        let (t2, ..) = ok("BSE-SENSEX-25Sep25-79500-PE");
        assert_eq!(t2, TYPE_PE);
    }

    #[test]
    fn junk_strike_token_is_structurally_accepted() {
        // Validity is Python's (`parse_strike_token`); Rust only locates it.
        let sym = "NSE-NIFTY-30Sep25-abc-CE";
        let (.., ss, se) = ok(sym);
        assert_eq!(&sym[ss..se], "abc");
    }

    #[test]
    fn hyphenated_underlying_right_anchored() {
        let sym = "NSE-BAJAJ-AUTO-30Sep25-FUT";
        let (_, _, us, ue, ..) = ok(sym);
        assert_eq!(&sym[us..ue], "BAJAJ-AUTO");
        let sym2 = "NSE-BAJAJ-AUTO-30Sep25-6000-CE";
        let (_, _, us2, ue2, _, _, _, ss, se) = ok(sym2);
        assert_eq!(&sym2[us2..ue2], "BAJAJ-AUTO");
        assert_eq!(&sym2[ss..se], "6000");
    }

    #[test]
    fn ampersand_names_parse() {
        let sym = "NSE-M&M-30Sep25-3000-CE";
        let (_, _, us, ue, ..) = ok(sym);
        assert_eq!(&sym[us..ue], "M&M");
        let sym2 = "NSE-M&MFIN-30Sep25-FUT";
        let (_, _, us2, ue2, ..) = ok(sym2);
        assert_eq!(&sym2[us2..ue2], "M&MFIN");
    }

    #[test]
    fn g1s_provenance_suffix_rejects() {
        none("NSE-NIFTY-30Sep25-24650-CE-G1S");
        none("NSE-NIFTY-30Sep25-FUT-G1S");
    }

    #[test]
    fn nifty_fut_alias_rejects() {
        none("NSE-NIFTY-FUT"); // 3 tokens < 4 — the §6 alias law
        none("NSE-30Sep25-FUT"); // 3 tokens
        none("NSE-NIFTY-30Sep25-CE"); // 4-token option < 5
    }

    #[test]
    fn type_token_case_sensitive() {
        none("NSE-NIFTY-30Sep25-fut");
        none("NSE-NIFTY-30Sep25-24650-ce");
        none("NSE-NIFTY-30Sep25-24650-Pe");
    }

    #[test]
    fn lowercase_exchange_parses_verbatim() {
        let sym = "nse-NIFTY-30Sep25-FUT";
        let (_, xe, ..) = ok(sym);
        assert_eq!(&sym[0..xe], "nse"); // oracle truth — exchange verbatim
    }

    #[test]
    fn empty_and_degenerate_reject() {
        none("");
        none("-");
        none("FUT"); // 1 token
        none("NSE--30Sep25-FUT"); // empty underlying
        none("-NIFTY-30Sep25-FUT"); // empty exchange
        none("NSE-NIFTY-30Sep25--CE"); // empty strike token
    }

    #[test]
    fn empty_interior_tokens_join_to_hyphen_underlying() {
        // parts[1:-2] = ["A", ""] joins to "A-" — NON-empty (oracle truth).
        let sym = "NSE-A--30Sep25-FUT";
        let (_, _, us, ue, ..) = ok(sym);
        assert_eq!(&sym[us..ue], "A-");
        // ["", ""] joins to "-" — also non-empty.
        let sym2 = "NSE---30Sep25-FUT";
        let (_, _, us2, ue2, ..) = ok(sym2);
        assert_eq!(&sym2[us2..ue2], "-");
    }

    #[test]
    fn whitespace_wrapped_symbol() {
        none("NSE-NIFTY-30Sep25-FUT "); // type token "FUT " mismatch
        let sym = " NSE-NIFTY-30Sep25-FUT"; // exchange " NSE" — oracle truth
        let (_, xe, ..) = ok(sym);
        assert_eq!(&sym[0..xe], " NSE");
    }

    #[test]
    fn space_day_arm_through_the_full_grammar() {
        let sym = "NSE-NIFTY- 3Sep25-FUT"; // ACCEPT — day 3
        let (.., y, m, d, _, _) = {
            let t = ok(sym);
            ((), (), t.4, t.5, t.6, (), ())
        };
        assert_eq!((y, m, d), (2025, 9, 3));
        none("NSE-NIFTY- 30Sep25-FUT"); // the reject twin
    }

    #[test]
    fn double_type_token_rejects() {
        none("NSE-NIFTY-30Sep25-FUT-FUT"); // expiry slot holds "FUT"
    }

    #[test]
    fn long_symbols() {
        let underlying = "A".repeat(1_000);
        let sym = format!("NSE-{underlying}-30Sep25-FUT");
        let (_, _, us, ue, ..) = ok(&sym);
        assert_eq!(ue - us, 1_000);
        let junk = "x".repeat(10_000);
        none(&junk);
    }

    #[test]
    fn localized_month_tokens_reject() {
        // Under the C/English law this crate mirrors (the facade's LOCALE
        // GUARD makes it the only locale ever engaged), `ene`/`okt` are
        // not months.
        none("NSE-NIFTY-30ene25-FUT");
        none("NSE-NIFTY-30Okt25-FUT");
    }

    #[test]
    fn non_ascii_input_never_panics_and_rejects() {
        // The facade never sends non-ASCII (pre-gate); byte-based matching
        // must still be total. Unicode digits are NOT ASCII digits here.
        none("NSE-NIFTY-30Sep٢٥-FUT");
        none("NSE-NIFTY-٣٠Sep25-FUT");
        none("NSE-NIFTY-30Ｓep25-FUT");
    }

    #[test]
    fn version_constants_mirror_python() {
        assert_eq!(EXPIRY_YEAR_MIN, 2019);
        assert_eq!(EXPIRY_YEAR_MAX, 2059);
        assert!(!crate::VERSION.is_empty());
    }

    // ------------------------------------------------------------------
    // Properties (proptest — never-panics, determinism, span sanity).
    // ------------------------------------------------------------------

    proptest! {
        #[test]
        fn never_panics_on_arbitrary_unicode(s in "\\PC*") {
            let _ = parse_fno_symbol_coords(&s);
        }

        #[test]
        fn never_panics_on_arbitrary_ascii(s in "[ -~]{0,64}") {
            let _ = parse_fno_symbol_coords(&s);
        }

        #[test]
        fn deterministic(s in "[ -~]{0,64}") {
            prop_assert_eq!(parse_fno_symbol_coords(&s), parse_fno_symbol_coords(&s));
        }

        #[test]
        fn accepted_spans_are_structurally_sound(s in "[ -~]{0,64}") {
            if let Some((t, xe, us, ue, y, m, d, ss, se)) = parse_fno_symbol_coords(&s) {
                let len = s.len();
                prop_assert!(t <= 2);
                prop_assert!(xe >= 1 && xe <= len);
                prop_assert_eq!(us, xe + 1); // the derived structural relation
                prop_assert!(us < ue && ue <= len);
                prop_assert!((EXPIRY_YEAR_MIN..=EXPIRY_YEAR_MAX).contains(&y));
                prop_assert!((1..=12).contains(&m));
                prop_assert!(d >= 1 && d <= days_in_month(m, y));
                if t == TYPE_FUT {
                    prop_assert_eq!((ss, se), (0, 0));
                } else {
                    prop_assert!(ss < se && se <= len);
                }
            }
        }
    }
}
