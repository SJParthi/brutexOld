//! Credential redaction for error messages and notifications.
//!
//! Prevents accidental leakage of secrets (client IDs, PINs, TOTP codes)
//! in log files, Telegram notifications, and SNS messages.
//!
//! # How It Works
//!
//! `redact_url_params` strips URL query parameters and known credential
//! patterns from arbitrary strings. It is regex-free — uses only
//! `str::find` and character scanning.
//!
//! # Usage
//!
//! Apply at error-creation sites (token_manager.rs) and at notification
//! boundaries (events.rs) for defense-in-depth.

/// Redacts URL query parameters and known credential patterns from a string.
///
/// Replaces query parameters in URLs (`?key=val&...`) with `?[REDACTED]`.
/// Also catches standalone `dhanClientId=`, `pin=`, `totp=` patterns
/// outside of URLs.
///
/// # Performance
///
/// Auth is cold path. String allocation is acceptable.
///
/// # Examples
///
/// ```
/// use tickvault_common::sanitize::redact_url_params;
///
/// let raw = "error for url (https://auth.dhan.co/app/gen?dhanClientId=123&pin=456)";
/// let safe = redact_url_params(raw);
/// assert!(!safe.contains("123"));
/// assert!(!safe.contains("456"));
/// assert!(safe.contains("?[REDACTED]"));
/// ```
pub fn redact_url_params(input: &str) -> String {
    if input.is_empty() {
        return String::new();
    }

    let mut result = redact_urls(input);
    result = redact_param_value(&result, "dhanClientId=");
    result = redact_param_value(&result, "pin=");
    result = redact_param_value(&result, "totp=");
    result
}

/// Replaces query parameters in URLs with `[REDACTED]`.
///
/// Scans for `https://` or `http://` prefixes, finds the `?` delimiter,
/// and replaces everything from `?` to the next whitespace, `)`, or
/// end-of-string with `?[REDACTED]`.
///
/// Uses `str::find` for UTF-8–safe scanning (no byte-level indexing).
fn redact_urls(input: &str) -> String {
    let mut result = String::with_capacity(input.len());
    let mut remaining = input;

    while !remaining.is_empty() {
        // Find the earliest URL prefix
        let url_start = match (remaining.find("http://"), remaining.find("https://")) {
            (Some(h), Some(hs)) => h.min(hs),
            (Some(h), None) => h,
            (None, Some(hs)) => hs,
            (None, None) => {
                result.push_str(remaining);
                break;
            }
        };

        // Copy everything before the URL
        result.push_str(&remaining[..url_start]);
        remaining = &remaining[url_start..];

        // Find the end of the URL (whitespace, ')' or end-of-string).
        // URLs are ASCII-only, so non-ASCII chars also terminate the URL.
        let url_end = remaining
            .find(|c: char| c.is_whitespace() || c == ')')
            .unwrap_or(remaining.len());

        let url = &remaining[..url_end];

        // Check if URL has query params
        if let Some(q_pos) = url.find('?') {
            result.push_str(&url[..q_pos]);
            result.push_str("?[REDACTED]");
        } else {
            result.push_str(url);
        }

        remaining = &remaining[url_end..];
    }

    result
}

/// Redacts the value portion of a `key=value` pattern.
///
/// Replaces `key=<value>` with `key=[REDACTED]` where `<value>` extends
/// to the next `&`, whitespace, `)`, or end-of-string.
///
/// Uses `str::find` for UTF-8–safe scanning (no byte-level indexing).
fn redact_param_value(input: &str, key: &str) -> String {
    let mut result = String::with_capacity(input.len());
    let mut search_from = 0;

    while let Some(pos) = input[search_from..].find(key) {
        let abs_pos = search_from.saturating_add(pos);
        // Copy everything before this match
        result.push_str(&input[search_from..abs_pos]);
        result.push_str(key);
        result.push_str("[REDACTED]");

        // Skip past the value using char-aware scanning
        let value_start = abs_pos.saturating_add(key.len());
        let value_end = input[value_start..]
            .find(|c: char| c == '&' || c.is_whitespace() || c == ')')
            .map(|i| value_start.saturating_add(i))
            .unwrap_or(input.len());

        search_from = value_end;
    }

    // Copy any remaining text after the last match
    result.push_str(&input[search_from..]);
    result
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_empty_string() {
        assert_eq!(redact_url_params(""), "");
    }

    #[test]
    fn test_no_url_no_params() {
        let input = "some plain error message";
        assert_eq!(redact_url_params(input), input);
    }

    #[test]
    fn test_url_without_query_params() {
        let input = "error for url (https://auth.dhan.co/app/generateAccessToken)";
        assert_eq!(redact_url_params(input), input);
    }

    #[test]
    fn test_url_with_query_params_redacted() {
        let input = "error sending request for url (https://auth.dhan.co/app/generateAccessToken?dhanClientId=1106656882&pin=785478&totp=782561)";
        let output = redact_url_params(input);
        assert!(
            !output.contains("1106656882"),
            "client ID must not appear: {output}"
        );
        assert!(!output.contains("785478"), "PIN must not appear: {output}");
        assert!(!output.contains("782561"), "TOTP must not appear: {output}");
        assert!(
            output.contains("?[REDACTED]"),
            "must contain redaction marker: {output}"
        );
        assert!(
            output.contains("generateAccessToken"),
            "URL path must be preserved: {output}"
        );
    }

    #[test]
    fn test_exact_reqwest_error_format() {
        // This is the exact format from the Telegram screenshot
        let input = "generateAccessToken request failed: error sending request for url (https://auth.dhan.co/app/generateAccessToken?dhanClientId=1106656882&pin=785478&totp=772509)";
        let output = redact_url_params(input);
        assert!(!output.contains("1106656882"));
        assert!(!output.contains("785478"));
        assert!(!output.contains("772509"));
        assert!(output.contains("generateAccessToken request failed"));
        assert!(output.contains("?[REDACTED]"));
    }

    #[test]
    fn test_multiple_urls_all_redacted() {
        let input = "tried https://a.com/x?secret=123 then https://b.com/y?key=456";
        let output = redact_url_params(input);
        assert!(!output.contains("123"), "first URL param leaked: {output}");
        assert!(!output.contains("456"), "second URL param leaked: {output}");
    }

    #[test]
    fn test_url_at_end_of_string() {
        let input = "error at https://api.com/path?token=secret123";
        let output = redact_url_params(input);
        assert!(
            !output.contains("secret123"),
            "param at end leaked: {output}"
        );
        assert!(output.contains("?[REDACTED]"));
    }

    #[test]
    fn test_http_url_also_redacted() {
        let input = "url (http://insecure.com/api?key=abc123)";
        let output = redact_url_params(input);
        assert!(
            !output.contains("abc123"),
            "http URL param leaked: {output}"
        );
    }

    #[test]
    fn test_standalone_dhan_client_id_redacted() {
        let input = "response contained dhanClientId=1106656882 in body";
        let output = redact_url_params(input);
        assert!(
            !output.contains("1106656882"),
            "standalone client ID leaked: {output}"
        );
        assert!(output.contains("dhanClientId=[REDACTED]"));
    }

    #[test]
    fn test_standalone_pin_redacted() {
        let input = "error: pin=785478 was invalid";
        let output = redact_url_params(input);
        assert!(
            !output.contains("785478"),
            "standalone PIN leaked: {output}"
        );
        assert!(output.contains("pin=[REDACTED]"));
    }

    #[test]
    fn test_standalone_totp_redacted() {
        let input = "TOTP rejected: totp=772509";
        let output = redact_url_params(input);
        assert!(
            !output.contains("772509"),
            "standalone TOTP leaked: {output}"
        );
        assert!(output.contains("totp=[REDACTED]"));
    }

    #[test]
    fn test_preserves_url_path() {
        let input = "error for url (https://auth.dhan.co/app/generateAccessToken?dhanClientId=123)";
        let output = redact_url_params(input);
        assert!(
            output.contains("/app/generateAccessToken"),
            "URL path lost: {output}"
        );
    }

    #[test]
    fn test_preserves_surrounding_text() {
        let input = "prefix https://x.com/a?b=c suffix";
        let output = redact_url_params(input);
        assert!(output.starts_with("prefix "), "prefix lost: {output}");
        assert!(output.ends_with(" suffix"), "suffix lost: {output}");
    }

    #[test]
    fn test_multibyte_utf8_em_dash_does_not_panic() {
        // Exact format from NotificationEvent — contains `—` (em dash, 3 bytes).
        let input = "attempt 1: Dhan authentication failed: generateAccessToken request failed: error sending request for url (https://auth.dhan.co/app/generateAccessToken?[REDACTED]) — retrying in 0s";
        let output = redact_url_params(input);
        assert!(output.contains("—"), "em dash must be preserved: {output}");
        assert!(
            output.contains("?[REDACTED]"),
            "URL must still be redacted: {output}"
        );
    }

    #[test]
    fn test_multibyte_utf8_around_url() {
        // Non-ASCII characters before and after a URL
        let input = "errör at https://x.com/api?key=secret — done";
        let output = redact_url_params(input);
        assert!(!output.contains("secret"), "param leaked: {output}");
        assert!(output.contains("errör"), "leading UTF-8 lost: {output}");
        assert!(output.contains("— done"), "trailing UTF-8 lost: {output}");
    }

    #[test]
    fn test_dhan_auth_full_error_with_body() {
        let input = "Dhan authentication failed: generateAccessToken request failed: error sending request for url (https://auth.dhan.co/app/generateAccessToken?dhanClientId=1106656882&pin=785478&totp=772509)";
        let output = redact_url_params(input);
        assert!(!output.contains("1106656882"));
        assert!(!output.contains("785478"));
        assert!(!output.contains("772509"));
        assert!(output.contains("Dhan authentication failed"));
    }

    #[test]
    fn test_both_http_and_https_urls_in_same_string() {
        // Exercises the (Some(h), Some(hs)) branch at line 64 where
        // both http:// and https:// are found in the remaining string.
        let input =
            "tried http://example.com/api?secret=abc then https://auth.dhan.co/app?token=xyz";
        let output = redact_url_params(input);
        assert!(
            !output.contains("secret=abc"),
            "http params leaked: {output}"
        );
        assert!(
            !output.contains("token=xyz"),
            "https params leaked: {output}"
        );
        assert!(output.contains("tried"), "prefix lost: {output}");
        assert!(output.contains("then"), "middle text lost: {output}");
    }

    #[test]
    fn test_http_before_https_redacts_both() {
        // http:// appears first — the min(h, hs) path picks it
        let input = "see http://a.com?k=v and https://b.com?s=t end";
        let output = redact_url_params(input);
        assert!(!output.contains("k=v"), "http param leaked: {output}");
        assert!(!output.contains("s=t"), "https param leaked: {output}");
    }

    #[test]
    fn test_https_before_http_redacts_both() {
        // https:// appears first — the min(h, hs) path picks it
        let input = "see https://b.com?s=t and http://a.com?k=v end";
        let output = redact_url_params(input);
        assert!(!output.contains("s=t"), "https param leaked: {output}");
        assert!(!output.contains("k=v"), "http param leaked: {output}");
    }
}
