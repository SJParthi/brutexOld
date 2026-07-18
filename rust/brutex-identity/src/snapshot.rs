//! The streamed `bxsnap2` data-snapshot digest — framing + timestamp render.
//!
//! The Rust home of `brutex/engine/snapshot_hash.py::data_snapshot_hash`
//! (DEC-SNAPSHOT-HASH-FULL-001 / DEC-RUST-RUNID-STORES-001): every loaded
//! bar's SEVEN fields fold into ONE streamed BLAKE3 under the `bxsnap2|`
//! domain tag, O(1) extra memory. The preimage framing (header, per-slice
//! header, per-bar line, `|`-joins, `\n` separators) and the ISO-8601
//! timestamp rendering live HERE; the Python wrapper supplies only the
//! primitive field texts CPython owns (`str(Decimal)` — the storage-exact
//! price text) plus the slice iteration order (`sorted(slices.items())`).
//!
//! Byte-parity with the retired Python derivation is PROVEN (the frozen
//! `bxsnap2` goldens + the pre-conversion oracle fixture pass unmodified),
//! so run identities (Golden Rule #4) are unchanged by the conversion.

use blake3::Hasher;

/// One streamed bxsnap2 digest in progress.
///
/// Mirrors the retired Python loop exactly: construct with the four scope
/// labels, then per slice (in the caller's sorted order) `add_slice`
/// followed by one `add_bar_line` per bar, then `hexdigest`.
pub struct SnapshotHasher {
    hasher: Hasher,
}

impl SnapshotHasher {
    /// Fold the `bxsnap2|{exchange}|{segment}|{start}|{end}` domain header.
    pub fn new(exchange: &str, segment: &str, start: &str, end: &str) -> Self {
        let mut hasher = Hasher::new();
        hasher.update(format!("bxsnap2|{exchange}|{segment}|{start}|{end}").as_bytes());
        SnapshotHasher { hasher }
    }

    /// Fold one slice header: `\n{symbol}|{interval}|{bar_count}`.
    pub fn add_slice(&mut self, symbol: &str, interval: &str, bar_count: u64) {
        self.hasher
            .update(format!("\n{symbol}|{interval}|{bar_count}").as_bytes());
    }

    /// Fold one bar line: `\n{ts}|{open}|{high}|{low}|{close}|{volume}|{oi}`.
    ///
    /// `iso_ts` is the CPython-`isoformat()`-shaped timestamp text (rendered
    /// by [`format_py_isoformat`]); the four prices arrive as the exact
    /// `str(Decimal)` texts; a `None` open-interest folds as the literal
    /// `"None"` (the Python f-string's rendering of `None`).
    #[allow(clippy::too_many_arguments)]
    pub fn add_bar_line(
        &mut self,
        iso_ts: &str,
        open: &str,
        high: &str,
        low: &str,
        close: &str,
        volume: i64,
        open_interest: Option<i64>,
    ) {
        let oi = match open_interest {
            None => "None".to_string(),
            Some(v) => v.to_string(),
        };
        self.hasher
            .update(format!("\n{iso_ts}|{open}|{high}|{low}|{close}|{volume}|{oi}").as_bytes());
    }

    /// Finalise to the 64-char lowercase BLAKE3 hex digest.
    pub fn hexdigest(&self) -> String {
        self.hasher.finalize().to_hex().to_string()
    }
}

/// Render a datetime exactly as CPython `datetime.isoformat()` does.
///
/// * base `YYYY-MM-DDTHH:MM:SS` (4-digit zero-padded year — Python years are
///   1..9999);
/// * `.ffffff` appended iff `microsecond != 0` (always 6 digits);
/// * for an AWARE datetime, the UTC offset as `±HH:MM`, plus `:SS` iff the
///   offset has a seconds component (Python renders sub-minute offsets);
///   a NAIVE datetime (`offset_seconds = None`) has no suffix.
///
/// Offsets with a MICROSECOND component (legal on a pathological Python
/// `tzinfo`, rendered `±HH:MM:SS.ffffff` by CPython) are REFUSED at the
/// PyO3 edge (`isoformat_of` rejects a sub-second-precision offset) — a
/// disclosed narrowing of DEC-RUST-RUNID-STORES-001 (no real calendar has
/// one; NSE/IST is a whole-minute offset).
#[allow(clippy::too_many_arguments)]
pub fn format_py_isoformat(
    year: i32,
    month: u32,
    day: u32,
    hour: u32,
    minute: u32,
    second: u32,
    microsecond: u32,
    offset_seconds: Option<i32>,
) -> String {
    let mut out = format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}");
    if microsecond != 0 {
        out.push_str(&format!(".{microsecond:06}"));
    }
    if let Some(total) = offset_seconds {
        let sign = if total < 0 { '-' } else { '+' };
        let magnitude = total.unsigned_abs();
        let hh = magnitude / 3600;
        let mm = (magnitude % 3600) / 60;
        let ss = magnitude % 60;
        out.push_str(&format!("{sign}{hh:02}:{mm:02}"));
        if ss != 0 {
            out.push_str(&format!(":{ss:02}"));
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::{SnapshotHasher, format_py_isoformat};

    /// Expected strings captured from CPython 3.12 `datetime.isoformat()`.
    #[test]
    fn isoformat_corpus() {
        // UTC bar open (the lake convention).
        assert_eq!(
            format_py_isoformat(2024, 1, 2, 3, 45, 0, 0, Some(0)),
            "2024-01-02T03:45:00+00:00"
        );
        // IST (+05:30) with microseconds.
        assert_eq!(
            format_py_isoformat(2024, 3, 5, 9, 15, 0, 123_456, Some(5 * 3600 + 30 * 60)),
            "2024-03-05T09:15:00.123456+05:30"
        );
        // Negative offset.
        assert_eq!(
            format_py_isoformat(2024, 1, 2, 10, 0, 0, 0, Some(-(4 * 3600 + 30 * 60))),
            "2024-01-02T10:00:00-04:30"
        );
        // Sub-minute offset renders the seconds component.
        assert_eq!(
            format_py_isoformat(2024, 1, 2, 10, 0, 0, 0, Some(3600 + 2 * 60 + 3)),
            "2024-01-02T10:00:00+01:02:03"
        );
        // Naive — no suffix; small year zero-pads to 4 digits.
        assert_eq!(
            format_py_isoformat(33, 12, 31, 23, 59, 59, 1, None),
            "0033-12-31T23:59:59.000001"
        );
    }

    /// The degenerate all-empty-slices digest is domain-tagged (a bxsnap2
    /// header alone hashes differently from raw blake3 of nothing).
    #[test]
    fn header_only_digest_is_domain_tagged() {
        let hasher = SnapshotHasher::new("NSE", "INDEX", "2024-01-01", "2024-02-01");
        let digest = hasher.hexdigest();
        assert_eq!(digest.len(), 64);
        assert_ne!(digest, blake3::hash(b"").to_hex().to_string());
    }

    /// The framing is position-sensitive: swapping two prices moves the digest.
    #[test]
    fn bar_line_field_order_matters() {
        let mut a = SnapshotHasher::new("NSE", "INDEX", "s", "e");
        a.add_slice("SYM", "1minute", 1);
        a.add_bar_line("t", "1.00", "2.00", "0.50", "1.50", 10, None);
        let mut b = SnapshotHasher::new("NSE", "INDEX", "s", "e");
        b.add_slice("SYM", "1minute", 1);
        b.add_bar_line("t", "2.00", "1.00", "0.50", "1.50", 10, None);
        assert_ne!(a.hexdigest(), b.hexdigest());
    }

    /// `None` open-interest folds as the literal "None", distinct from 0.
    #[test]
    fn none_oi_distinct_from_zero() {
        let mut a = SnapshotHasher::new("NSE", "FNO", "s", "e");
        a.add_slice("SYM", "1minute", 1);
        a.add_bar_line("t", "1.00", "1.00", "1.00", "1.00", 0, None);
        let mut b = SnapshotHasher::new("NSE", "FNO", "s", "e");
        b.add_slice("SYM", "1minute", 1);
        b.add_bar_line("t", "1.00", "1.00", "1.00", "1.00", 0, Some(0));
        assert_ne!(a.hexdigest(), b.hexdigest());
    }
}
