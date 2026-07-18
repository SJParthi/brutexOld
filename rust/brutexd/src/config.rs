//! brutexd startup configuration — argv parse + fail-loud validation.
//!
//! The flag DECISION (spawn brutexd at all?) lives Python-side in
//! `brutex/dashboard/rust_front.py` (`BRUTEX_RUST_FRONT`, house env-law:
//! junk value → loud `CliUserError` exit 1). This module only parses the
//! concrete wiring the Python launcher passes on the command line:
//!
//! ```text
//! brutexd --listen 127.0.0.1:9080 --upstream-port 43211 --static-dir <dir>
//! ```
//!
//! Every violation is a loud startup REFUSAL (`Err` → exit code 2 in
//! `main`), never a silent fallback:
//!
//! * `--listen` must be a loopback address (127.0.0.0/8 or ::1) — the
//!   dashboard has NO auth; widening the bind is out of scope and refused
//!   (roadmap §3 hostile-review bullet, test-pinned).
//! * `--upstream-port` must be a non-zero u16.
//! * `--static-dir` must exist and be a directory.

use std::net::SocketAddr;
use std::path::PathBuf;

/// Validated brutexd configuration.
#[derive(Debug, Clone)]
pub struct Config {
    /// Operator-facing listen address (loopback-only, enforced).
    pub listen: SocketAddr,
    /// Loopback port of the FastAPI job host to proxy to.
    pub upstream_port: u16,
    /// Directory served under `/static/*`.
    pub static_dir: PathBuf,
}

/// Parse + validate argv (without the program name).
///
/// # Errors
///
/// Returns a human-readable message for any missing/unknown/invalid
/// argument; `main` prints it to stderr and exits 2 (fail-loud, never a
/// silent default).
pub fn parse_args(args: &[String]) -> Result<Config, String> {
    let mut listen: Option<String> = None;
    let mut upstream_port: Option<String> = None;
    let mut static_dir: Option<String> = None;

    let mut it = args.iter();
    while let Some(arg) = it.next() {
        let slot = match arg.as_str() {
            "--listen" => &mut listen,
            "--upstream-port" => &mut upstream_port,
            "--static-dir" => &mut static_dir,
            other => return Err(format!("unknown argument {other:?}")),
        };
        match it.next() {
            Some(value) => *slot = Some(value.clone()),
            None => return Err(format!("missing value for {arg}")),
        }
    }

    let listen_raw = listen.ok_or("missing required --listen <ip:port>")?;
    let listen: SocketAddr = listen_raw
        .parse()
        .map_err(|e| format!("invalid --listen {listen_raw:?}: {e}"))?;
    if !listen.ip().is_loopback() {
        return Err(format!(
            "refusing non-loopback --listen {listen} — brutexd is 127.0.0.1-only \
             (the dashboard has no auth; widening the bind is out of scope for Wave 1)"
        ));
    }

    let upstream_raw = upstream_port.ok_or("missing required --upstream-port <port>")?;
    let upstream_port: u16 = upstream_raw
        .parse()
        .map_err(|e| format!("invalid --upstream-port {upstream_raw:?}: {e}"))?;
    if upstream_port == 0 {
        return Err("invalid --upstream-port 0 — must be a concrete port".to_owned());
    }

    let static_raw = static_dir.ok_or("missing required --static-dir <dir>")?;
    let static_dir = PathBuf::from(&static_raw);
    if !static_dir.is_dir() {
        return Err(format!("--static-dir {static_raw:?} is not a directory"));
    }

    Ok(Config {
        listen,
        upstream_port,
        static_dir,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn argv(parts: &[&str]) -> Vec<String> {
        parts.iter().map(|s| (*s).to_owned()).collect()
    }

    fn valid_static_dir() -> String {
        // The crate's own src/ dir always exists during tests.
        concat!(env!("CARGO_MANIFEST_DIR"), "/src").to_owned()
    }

    #[test]
    fn parses_a_valid_config() {
        let dir = valid_static_dir();
        let cfg = parse_args(&argv(&[
            "--listen",
            "127.0.0.1:9080",
            "--upstream-port",
            "4321",
            "--static-dir",
            &dir,
        ]))
        .unwrap();
        assert_eq!(cfg.listen.port(), 9080);
        assert!(cfg.listen.ip().is_loopback());
        assert_eq!(cfg.upstream_port, 4321);
        assert!(cfg.static_dir.is_dir());
    }

    #[test]
    fn accepts_the_python_launcher_rendered_loopback_forms() {
        // MED-1 regression: the Python launcher
        // (`brutex/dashboard/rust_front.render_listen`) maps every
        // LOOPBACK_HOSTS member to a token this parser accepts —
        // `localhost` -> 127.0.0.1 and `::1` -> the RFC 3986 bracket form
        // `[::1]`. A bare `localhost:9080` / `::1:9080` would be REJECTED as
        // a numeric SocketAddr, so the launcher MUST render these exact
        // shapes; pin that they parse + stay loopback here.
        let dir = valid_static_dir();
        for rendered in ["127.0.0.1:9080", "[::1]:9080"] {
            let cfg = parse_args(&argv(&[
                "--listen",
                rendered,
                "--upstream-port",
                "4321",
                "--static-dir",
                &dir,
            ]))
            .unwrap_or_else(|e| panic!("rejected launcher-rendered {rendered:?}: {e}"));
            assert!(cfg.listen.ip().is_loopback(), "not loopback: {rendered}");
            assert_eq!(cfg.listen.port(), 9080);
        }
    }

    #[test]
    fn refuses_the_pre_fix_bare_loopback_spellings() {
        // The exact forms that DID reach config.rs before MED-1 — proof the
        // fix was load-bearing: a bare `localhost`/`::1` authority does NOT
        // parse as a numeric SocketAddr.
        let dir = valid_static_dir();
        for bad in ["localhost:9080", "::1:9080"] {
            let err = parse_args(&argv(&[
                "--listen",
                bad,
                "--upstream-port",
                "4321",
                "--static-dir",
                &dir,
            ]))
            .unwrap_err();
            assert!(err.contains("invalid --listen"), "got: {err}");
        }
    }

    #[test]
    fn refuses_non_loopback_listen() {
        let dir = valid_static_dir();
        let err = parse_args(&argv(&[
            "--listen",
            "0.0.0.0:9080",
            "--upstream-port",
            "4321",
            "--static-dir",
            &dir,
        ]))
        .unwrap_err();
        assert!(err.contains("non-loopback"), "got: {err}");
    }

    #[test]
    fn refuses_unknown_argument() {
        let err = parse_args(&argv(&["--bogus", "x"])).unwrap_err();
        assert!(err.contains("unknown argument"), "got: {err}");
    }

    #[test]
    fn refuses_missing_value() {
        let err = parse_args(&argv(&["--listen"])).unwrap_err();
        assert!(err.contains("missing value"), "got: {err}");
    }

    #[test]
    fn refuses_missing_required_flags() {
        assert!(parse_args(&[]).unwrap_err().contains("--listen"));
        let err = parse_args(&argv(&["--listen", "127.0.0.1:1"])).unwrap_err();
        assert!(err.contains("--upstream-port"), "got: {err}");
        let err =
            parse_args(&argv(&["--listen", "127.0.0.1:1", "--upstream-port", "2"])).unwrap_err();
        assert!(err.contains("--static-dir"), "got: {err}");
    }

    #[test]
    fn refuses_bad_values() {
        let dir = valid_static_dir();
        assert!(
            parse_args(&argv(&[
                "--listen",
                "not-an-addr",
                "--upstream-port",
                "1",
                "--static-dir",
                &dir
            ]))
            .unwrap_err()
            .contains("invalid --listen")
        );
        assert!(
            parse_args(&argv(&[
                "--listen",
                "127.0.0.1:1",
                "--upstream-port",
                "zero",
                "--static-dir",
                &dir
            ]))
            .unwrap_err()
            .contains("invalid --upstream-port")
        );
        assert!(
            parse_args(&argv(&[
                "--listen",
                "127.0.0.1:1",
                "--upstream-port",
                "0",
                "--static-dir",
                &dir
            ]))
            .unwrap_err()
            .contains("--upstream-port 0")
        );
        assert!(
            parse_args(&argv(&[
                "--listen",
                "127.0.0.1:1",
                "--upstream-port",
                "2",
                "--static-dir",
                "/definitely/not/a/dir"
            ]))
            .unwrap_err()
            .contains("not a directory")
        );
    }
}
