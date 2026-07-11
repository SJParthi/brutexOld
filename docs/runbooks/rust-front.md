# Rust front (`brutexd`) — `BRUTEX_RUST_FRONT` runbook

**Status:** Wave 1 of the Rust-uniformity program (roadmap PR-1). Default
**OFF**. Loopback-only.

## What the flag does

`BRUTEX_RUST_FRONT` controls the dashboard's process topology at
`brutex dashboard` / `brutex run` startup (resolved once, fail-loud):

| Value | Behaviour |
| --- | --- |
| absent / empty / `off` | **Default.** Single process: uvicorn binds the operator port directly. Byte-identical to the pre-brutexd dashboard — no Rust process exists. |
| `on` | Two processes: `brutexd` (Rust, axum) binds the operator port `127.0.0.1`-only; uvicorn binds a freshly allocated **private** loopback port. brutexd serves `/static/*` + `/health` natively and reverse-proxies **every other path verbatim** to uvicorn. |
| anything else | Loud refusal — `[brutex] invalid BRUTEX_RUST_FRONT=…` exit 1. Never a silent fallback. |

The Python seam is `brutex/dashboard/rust_front.py` (flag parse, binary
lookup, port allocation, supervised spawn/teardown); `cmd_dashboard` only
threads the decision. The binary is found via `BRUTEX_BRUTEXD_BIN` (explicit
override) → `rust/brutexd/target/{release,debug}/brutexd` → `PATH`; missing
binary = loud refusal naming the build command:

```sh
cargo build --release --manifest-path rust/brutexd/Cargo.toml
BRUTEX_RUST_FRONT=on brutex dashboard
```

## Rollback

Unset the flag (or set `off`) and restart — one env var, no deploy, no code
change. The Python `/static` mount survives this PR untouched, so the
single-process topology is always available.

## The parity guarantee

`tests/integration/test_sidecar_parity_static.py` (plus the crate's own
`rust/brutexd/tests/integration.rs`) boots BOTH processes over the same tree
and pins:

- **static:** every file under `brutex/dashboard/static/` — sha256(body),
  `content-type`, `content-length` equal to the Starlette mount;
  `cache-control` equally absent;
- **health:** `/health` = the pinned literal `brutexd ok\n` (a NEW route —
  the FastAPI app serves no health route today; with the flag OFF nothing
  serves `/health`);
- **proxy transparency:** for a hostile corpus (200 JSON, 404, real FastAPI
  422, 307 redirect not followed + `Location` verbatim, chunked stream with
  no fabricated `content-length`, POST with body, percent-encoded query,
  trailing slash unnormalized), the front response equals the direct
  response in status, body bytes, and the FULL header map minus the
  RFC 7230 §6.1 hop-by-hop set (`connection`, `keep-alive`,
  `proxy-authenticate`, `proxy-authorization`, `te`, `trailer`,
  `transfer-encoding`, `upgrade` — per-hop framing/connection management,
  re-derived by each hop by design) and minus the `date` value (per-response
  wall clock; presence still compared);
- **leanness:** `rust/brutexd/Cargo.toml` declares no duckdb / polars /
  parquet / arrow — brutexd sits in front of the substrate and never opens
  the lake in this wave.

## Honest limits (Golden Rule #10)

- **No latency claim.** This is a *uniformity program, not a performance
  program* (roadmap §0) — no performance number was measured or is claimed.
- **Loopback-only.** `BRUTEX_RUST_FRONT=on` with a non-loopback `--host` is
  refused loudly. To serve beyond loopback, unset the flag.
- **Boot window:** brutexd starts before uvicorn finishes binding; proxied
  paths answer a plain-body 502 (`brutexd: upstream unreachable …`) for that
  sub-second window — a visible degrade, never a hang. The richer
  "job host restarting" 503 class is the wave-16 end-state refinement.
- **Conditional requests:** brutexd's static serving does not replicate
  Starlette's `etag`/`last-modified` in Wave 1 — conditional requests get a
  full 200 (correct, never stale, just less cacheable).
- **Supervision residual:** brutexd is terminated in a `finally` when
  uvicorn exits or raises; a SIGKILLed parent cannot run its `finally`, so a
  hard-killed `brutex dashboard` can leave a brutexd child serving 502s
  until killed manually (`pkill brutexd`).
- **Port allocation TOCTOU:** the private uvicorn port is allocated by
  bind-to-0-then-release; if another process steals it in the window,
  uvicorn's own bind fails loudly (the same failure mode a fixed port has).
