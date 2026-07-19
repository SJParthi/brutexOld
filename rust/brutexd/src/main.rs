//! brutexd binary entrypoint — parse argv, bind 127.0.0.1-only, serve.
//!
//! Thin by design: every behaviour lives in the library (`build_router` +
//! modules) where the integration tests drive it in-process. This file only
//! wires argv → config → listener → serve, with fail-loud exits:
//!
//! * exit 2 — invalid arguments / refused config (message on stderr),
//! * exit 1 — bind or serve failure (message on stderr).
//!
//! Lifecycle: the Python launcher (`brutex/dashboard/rust_front.py`) spawns
//! this process AFTER allocating the private upstream port and terminates it
//! when uvicorn exits (supervised child, never an orphan by design; a
//! SIGKILLed parent leaves the child to die on the next request cycle via
//! operator action — documented in docs/runbooks/rust-front.md).

use std::process::ExitCode;

fn main() -> ExitCode {
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_target(false)
        .init();

    let args: Vec<String> = std::env::args().skip(1).collect();
    let cfg = match brutexd::config::parse_args(&args) {
        Ok(cfg) => cfg,
        Err(msg) => {
            eprintln!("brutexd: {msg}");
            return ExitCode::from(2);
        }
    };

    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(err) => {
            eprintln!("brutexd: failed to build tokio runtime: {err}");
            return ExitCode::from(1);
        }
    };

    let result: Result<(), String> = runtime.block_on(async {
        let listener = tokio::net::TcpListener::bind(cfg.listen)
            .await
            .map_err(|err| format!("failed to bind {}: {err}", cfg.listen))?;
        tracing::info!(
            listen = %cfg.listen,
            upstream_port = cfg.upstream_port,
            static_dir = %cfg.static_dir.display(),
            "brutexd.ready"
        );
        let router = brutexd::build_router(&cfg);
        axum::serve(listener, router)
            .await
            .map_err(|err| format!("serve failed: {err}"))
    });

    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(msg) => {
            eprintln!("brutexd: {msg}");
            ExitCode::from(1)
        }
    }
}
