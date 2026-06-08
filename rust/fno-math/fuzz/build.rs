//! Link libpython into the fuzz BINARY so the PyO3 C-API symbols resolve.
//!
//! `fno-math` is a PyO3 `extension-module` crate: PyO3 deliberately does NOT
//! emit a libpython link directive (the produced cdylib loads under any host
//! CPython at import time). The crate's hot kernels are only reachable through
//! the `#[pyfunction]` surface, which references Python C-API symbols
//! (`PyTuple_GetItem`, `PyBytes_*`, …) on its object-conversion paths.
//!
//! A normal `cargo test` dodges this by testing the *private* pure cores; a
//! fuzz EXECUTABLE that calls the public `#[pyfunction]`s cannot, so we must
//! provide the symbols. We link libpython here (the fuzz crate is a separate,
//! non-published package — production builds are untouched). The fuzzed code
//! path is pure integer work and never initialises the interpreter, so merely
//! resolving the symbols is sufficient.
//!
//! Link flags come from `python3.12-config` (falling back to `python3-config`),
//! so the path is discovered, not hard-coded. If neither is found the build
//! still emits the conventional `-lpython3.12`, which the linker resolves
//! against a system libpython on the default search path.

use std::process::Command;

fn main() {
    let ldflags = ["python3.12-config", "python3-config"]
        .iter()
        .find_map(|cfg| {
            Command::new(cfg)
                .args(["--ldflags", "--embed"])
                .output()
                .ok()
                .filter(|o| o.status.success())
                .and_then(|o| String::from_utf8(o.stdout).ok())
        })
        .unwrap_or_default();

    let mut linked_lib = false;
    for tok in ldflags.split_whitespace() {
        if let Some(path) = tok.strip_prefix("-L") {
            println!("cargo:rustc-link-search=native={path}");
        } else if let Some(lib) = tok.strip_prefix("-l") {
            // Link only libpython itself; -ldl / -lm are pulled in by std.
            if lib.starts_with("python") {
                println!("cargo:rustc-link-lib=dylib={lib}");
                linked_lib = true;
            }
        }
    }

    if !linked_lib {
        // Fallback: rely on the system search path for a versioned libpython.
        println!("cargo:rustc-link-lib=dylib=python3.12");
    }

    println!("cargo:rerun-if-changed=build.rs");
}
