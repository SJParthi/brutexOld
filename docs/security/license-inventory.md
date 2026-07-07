# Third-Party License Inventory

> Generated 2026-07-06 as part of the supply-chain audit (Workstream 5).
> Python: `pip-licenses` 5.x over a fresh Python 3.12 uv venv with
> `brutex[data,ui,optimize,groww,token-broker,dev]` installed (resolution as of 2026-07-06 —
> versions float within pyproject bounds, so re-generate when bounds change).
> Rust: `cargo metadata --locked` over the checked-in `Cargo.lock` of both crates.
> See `SUPPLY_CHAIN_POSTURE.md` for the analysis and verdict.

## Project's own license

- `pyproject.toml`: `license = { text = "Proprietary" }`
- `rust/trading-core/Cargo.toml` + `rust/fno-math/Cargo.toml`: `license = "Proprietary"`
- **No `LICENSE` file exists at the repo root** (see posture report, finding L-1).

## Python dependencies (138 packages, incl. brutex itself)

| Package | Version | License | Flag |
|---|---|---|---|
| aiohappyeyeballs | 2.7.1 | Python Software Foundation License |  |
| aiohttp | 3.14.1 | Apache-2.0 AND MIT |  |
| aiosignal | 1.4.0 | Apache Software License |  |
| annotated-doc | 0.0.4 | MIT |  |
| annotated-types | 0.7.0 | MIT License |  |
| anyio | 4.14.1 | MIT |  |
| args | 0.1.0 | BSD License |  |
| ast_serialize | 0.6.0 | MIT |  |
| attrs | 26.1.0 | MIT |  |
| Authlib | 1.7.2 | BSD License |  |
| bandit | 1.9.4 | Apache-2.0 |  |
| beautifulsoup4 | 4.15.0 | MIT License |  |
| blake3 | 1.0.9 | CC0-1.0 OR Apache-2.0 |  |
| boolean.py | 5.0 | BSD-2-Clause |  |
| boto3 | 1.43.40 | Apache-2.0 |  |
| botocore | 1.43.40 | Apache-2.0 |  |
| brutex | 0.2.0a0 | Proprietary | PROJECT |
| CacheControl | 0.14.4 | Apache-2.0 |  |
| certifi | 2025.11.12 | Mozilla Public License 2.0 (MPL 2.0) |  |
| cffi | 2.0.0 | MIT |  |
| charset-normalizer | 3.4.7 | MIT |  |
| click | 8.4.2 | BSD-3-Clause |  |
| clint | 0.5.1 | ISC License (ISCL) |  |
| cma | 4.4.4 | BSD-3-Clause |  |
| colorama | 0.4.6 | BSD License |  |
| coverage | 7.15.0 | Apache-2.0 |  |
| cryptography | 49.0.0 | Apache-2.0 OR BSD-3-Clause |  |
| cyclonedx-python-lib | 11.11.0 | Apache Software License |  |
| defusedxml | 0.7.1 | Python Software Foundation License |  |
| dparse | 0.6.4 | MIT License |  |
| duckdb | 1.5.4 | MIT License |  |
| fastapi | 0.139.0 | MIT |  |
| fastbloom_rs | 0.5.10 | Apache-2.0 |  |
| filelock | 3.29.5 | MIT |  |
| frozenlist | 1.8.0 | Apache-2.0 |  |
| google | 3.0.0 | BSD License |  |
| growwapi | 1.5.0 | MIT License |  |
| h11 | 0.16.0 | MIT License |  |
| httpcore | 1.0.9 | BSD-3-Clause |  |
| httpcore2 | 2.5.0 | BSD-3-Clause |  |
| httptools | 0.8.0 | MIT |  |
| httpx | 0.28.1 | BSD License |  |
| httpx2 | 2.5.0 | BSD-3-Clause |  |
| hypothesis | 6.156.1 | MPL-2.0 |  |
| idna | 3.18 | BSD-3-Clause |  |
| iniconfig | 2.3.0 | MIT |  |
| interrogate | 1.7.0 | MIT License |  |
| Jinja2 | 3.1.6 | BSD License |  |
| jmespath | 1.1.0 | MIT License |  |
| joblib | 1.5.3 | BSD-3-Clause |  |
| joserfc | 1.7.2 | BSD License |  |
| libcst | 1.8.6 | MIT License |  |
| librt | 0.12.0 | MIT |  |
| license-expression | 30.4.4 | Apache-2.0 |  |
| linkify-it-py | 2.1.0 | MIT License |  |
| lmdb | 2.2.1 | OLDAP-2.8 |  |
| markdown-it-py | 4.2.0 | MIT License |  |
| MarkupSafe | 3.0.3 | BSD-3-Clause |  |
| marshmallow | 4.3.0 | MIT |  |
| mdit-py-plugins | 0.6.1 | MIT License |  |
| mdurl | 0.1.2 | MIT License |  |
| msgpack | 1.2.1 | Apache-2.0 |  |
| multidict | 6.7.1 | Apache License 2.0 |  |
| mutmut | 3.6.0 | BSD-3-Clause |  |
| mypy | 2.1.0 | MIT |  |
| mypy_extensions | 1.1.0 | MIT |  |
| nats-py | 2.15.0 | Apache-2.0 |  |
| nkeys | 0.2.1 | Apache Software License |  |
| nltk | 3.9.4 | Apache Software License |  |
| nodeenv | 1.10.0 | BSD License |  |
| numpy | 2.5.1 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |  |
| packageurl-python | 0.17.6 | MIT License |  |
| packaging | 26.2 | Apache-2.0 OR BSD-2-Clause |  |
| pandas | 2.3.3 | BSD License |  |
| pathspec | 1.1.1 | Mozilla Public License 2.0 (MPL 2.0) |  |
| pip-api | 0.0.34 | Apache Software License |  |
| pip-requirements-parser | 32.0.1 | MIT |  |
| pip_audit | 2.10.1 | Apache Software License |  |
| platformdirs | 4.10.0 | MIT |  |
| pluggy | 1.6.0 | MIT License |  |
| polars | 1.42.1 | MIT License |  |
| polars-runtime-32 | 1.42.1 | MIT License |  |
| propcache | 0.5.2 | Apache Software License |  |
| protobuf | 5.29.6 | 3-Clause BSD License |  |
| py | 1.11.0 | MIT License |  |
| py-cpuinfo | 9.0.0 | MIT License |  |
| py-serializable | 2.1.0 | Apache Software License |  |
| pyarrow | 24.0.0 | Apache-2.0 |  |
| pycparser | 3.0 | BSD-3-Clause |  |
| pydantic | 2.13.4 | MIT |  |
| pydantic_core | 2.46.4 | MIT |  |
| Pygments | 2.20.0 | BSD-2-Clause |  |
| PyNaCl | 1.6.2 | Apache Software License |  |
| PyOTP | 2.10.0 | MIT |  |
| pyparsing | 3.3.2 | MIT |  |
| pyright | 1.1.411 | MIT |  |
| pytest | 9.1.1 | MIT |  |
| pytest-benchmark | 5.2.3 | BSD-2-Clause |  |
| pytest-cov | 7.1.0 | MIT |  |
| python-dateutil | 2.9.0.post0 | Apache Software License; BSD License |  |
| python-dotenv | 1.2.2 | BSD-3-Clause |  |
| pytz | 2026.2 | MIT License |  |
| PyYAML | 6.0.3 | MIT License |  |
| regex | 2026.6.28 | Apache-2.0 AND CNRI-Python |  |
| requests | 2.34.2 | Apache Software License |  |
| rich | 15.0.0 | MIT License |  |
| ruamel.yaml | 0.19.1 | MIT License |  |
| ruff | 0.15.20 | MIT |  |
| s3transfer | 0.19.0 | Apache Software License |  |
| safety | 3.8.1 | MIT |  |
| safety-schemas | 0.0.16 | MIT License |  |
| setproctitle | 1.3.7 | BSD License |  |
| shellingham | 1.5.4 | ISC License (ISCL) |  |
| six | 1.17.0 | MIT License |  |
| sortedcontainers | 2.4.0 | Apache Software License |  |
| soupsieve | 2.8.4 | MIT |  |
| starlette | 1.3.1 | BSD-3-Clause |  |
| stevedore | 5.9.0 | Apache-2.0 |  |
| structlog | 26.1.0 | MIT OR Apache-2.0 |  |
| tabulate | 0.10.0 | MIT |  |
| tenacity | 9.1.4 | Apache Software License |  |
| textual | 8.2.8 | MIT License |  |
| tomli | 2.4.1 | MIT |  |
| tomli_w | 1.2.0 | MIT License |  |
| tomlkit | 0.15.0 | MIT License |  |
| tqdm | 4.68.3 | MPL-2.0 AND MIT |  |
| truststore | 0.10.4 | MIT |  |
| typer | 0.25.1 | MIT |  |
| typing-inspection | 0.4.2 | MIT |  |
| typing_extensions | 4.16.0 | PSF-2.0 |  |
| tzdata | 2026.2 | Apache-2.0 |  |
| uc-micro-py | 2.0.0 | MIT License |  |
| urllib3 | 2.7.0 | MIT |  |
| uvicorn | 0.50.2 | BSD-3-Clause |  |
| uvloop | 0.22.1 | Apache Software License; MIT License |  |
| watchfiles | 1.2.0 | MIT License |  |
| websockets | 16.0 | BSD-3-Clause |  |
| yarl | 1.24.2 | Apache-2.0 |  |

## Rust dependencies (145 unique crate versions across both crates, dev/build deps included)

| Crate | Version | License (SPDX) | Used by | Flag |
|---|---|---|---|---|
| android_system_properties | 0.1.5 | MIT/Apache-2.0 | fno-math |  |
| anyhow | 1.0.103 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| arrayref | 0.3.9 | BSD-2-Clause | fno-math |  |
| arrayvec | 0.7.6 | MIT OR Apache-2.0 | fno-math |  |
| autocfg | 1.5.1 | Apache-2.0 OR MIT | fno-math, trading-core |  |
| bit-set | 0.8.0 | Apache-2.0 OR MIT | fno-math, trading-core |  |
| bit-vec | 0.8.0 | Apache-2.0 OR MIT | fno-math, trading-core |  |
| bitflags | 2.12.1 | MIT OR Apache-2.0 | fno-math |  |
| bitflags | 2.13.0 | MIT OR Apache-2.0 | trading-core |  |
| blake3 | 1.8.5 | CC0-1.0 OR Apache-2.0 OR Apache-2.0 WITH LLVM-exception | fno-math |  |
| bumpalo | 3.20.3 | MIT OR Apache-2.0 | fno-math |  |
| bytecheck | 0.8.2 | MIT | fno-math |  |
| bytecheck_derive | 0.8.2 | MIT | fno-math |  |
| bytes | 1.11.1 | MIT | fno-math |  |
| cc | 1.2.63 | MIT OR Apache-2.0 | fno-math |  |
| cfg-if | 1.0.4 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| chrono | 0.4.45 | MIT OR Apache-2.0 | fno-math |  |
| chrono-tz | 0.10.4 | MIT OR Apache-2.0 | fno-math |  |
| constant_time_eq | 0.4.2 | CC0-1.0 OR MIT-0 OR Apache-2.0 | fno-math |  |
| core-foundation-sys | 0.8.7 | MIT OR Apache-2.0 | fno-math |  |
| cpufeatures | 0.3.0 | MIT OR Apache-2.0 | fno-math |  |
| crossbeam-deque | 0.8.6 | MIT OR Apache-2.0 | fno-math |  |
| crossbeam-epoch | 0.9.18 | MIT OR Apache-2.0 | fno-math |  |
| crossbeam-utils | 0.8.21 | MIT OR Apache-2.0 | fno-math |  |
| either | 1.16.0 | MIT OR Apache-2.0 | fno-math |  |
| equivalent | 1.0.2 | Apache-2.0 OR MIT | fno-math, trading-core |  |
| errno | 0.3.14 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| fastrand | 2.4.1 | Apache-2.0 OR MIT | fno-math, trading-core |  |
| find-msvc-tools | 0.1.9 | MIT OR Apache-2.0 | fno-math |  |
| fno-math | 0.2.0 | Proprietary | fno-math | PROJECT |
| fnv | 1.0.7 | Apache-2.0 / MIT | fno-math, trading-core |  |
| foldhash | 0.1.5 | Zlib | fno-math, trading-core |  |
| futures-core | 0.3.32 | MIT OR Apache-2.0 | fno-math |  |
| futures-task | 0.3.32 | MIT OR Apache-2.0 | fno-math |  |
| futures-util | 0.3.32 | MIT OR Apache-2.0 | fno-math |  |
| getrandom | 0.3.4 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| getrandom | 0.4.2 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| hashbrown | 0.15.5 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| hashbrown | 0.17.1 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| heck | 0.5.0 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| iana-time-zone | 0.1.65 | MIT OR Apache-2.0 | fno-math |  |
| iana-time-zone-haiku | 0.1.2 | MIT OR Apache-2.0 | fno-math |  |
| id-arena | 2.3.0 | MIT/Apache-2.0 | fno-math, trading-core |  |
| indexmap | 2.14.0 | Apache-2.0 OR MIT | fno-math, trading-core |  |
| itoa | 1.0.18 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| jaeckel | 0.2.0 | MIT | trading-core |  |
| js-sys | 0.3.99 | MIT OR Apache-2.0 | fno-math |  |
| leb128fmt | 0.1.0 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| libc | 0.2.186 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| linux-raw-sys | 0.12.1 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | fno-math, trading-core |  |
| log | 0.4.31 | MIT OR Apache-2.0 | fno-math |  |
| log | 0.4.32 | MIT OR Apache-2.0 | trading-core |  |
| memchr | 2.8.1 | Unlicense OR MIT | fno-math, trading-core |  |
| munge | 0.4.7 | MIT | fno-math |  |
| munge_macro | 0.4.7 | MIT | fno-math |  |
| num-traits | 0.2.19 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| once_cell | 1.21.4 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| phf | 0.12.1 | MIT | fno-math |  |
| phf_shared | 0.12.1 | MIT | fno-math |  |
| pin-project-lite | 0.2.17 | Apache-2.0 OR MIT | fno-math |  |
| portable-atomic | 1.13.1 | Apache-2.0 OR MIT | fno-math |  |
| ppv-lite86 | 0.2.21 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| prettyplease | 0.2.37 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| proc-macro2 | 1.0.106 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| proptest | 1.11.0 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| ptr_meta | 0.3.1 | MIT | fno-math |  |
| ptr_meta_derive | 0.3.1 | MIT | fno-math |  |
| pyo3 | 0.29.0 | MIT OR Apache-2.0 | fno-math |  |
| pyo3-build-config | 0.29.0 | MIT OR Apache-2.0 | fno-math |  |
| pyo3-ffi | 0.29.0 | MIT OR Apache-2.0 | fno-math |  |
| pyo3-macros | 0.29.0 | MIT OR Apache-2.0 | fno-math |  |
| pyo3-macros-backend | 0.29.0 | MIT OR Apache-2.0 | fno-math |  |
| quick-error | 1.2.3 | MIT/Apache-2.0 | fno-math, trading-core |  |
| quote | 1.0.45 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| r-efi | 5.3.0 | MIT OR Apache-2.0 OR LGPL-2.1-or-later | fno-math, trading-core |  |
| r-efi | 6.0.0 | MIT OR Apache-2.0 OR LGPL-2.1-or-later | fno-math, trading-core |  |
| rancor | 0.1.1 | MIT | fno-math |  |
| rand | 0.9.4 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| rand_chacha | 0.9.0 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| rand_core | 0.9.5 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| rand_xorshift | 0.4.0 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| rayon | 1.12.0 | MIT OR Apache-2.0 | fno-math |  |
| rayon-core | 1.13.0 | MIT OR Apache-2.0 | fno-math |  |
| regex-syntax | 0.8.10 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| rend | 0.5.3 | MIT | fno-math |  |
| rkyv | 0.8.17 | MIT | fno-math |  |
| rkyv_derive | 0.8.17 | MIT | fno-math |  |
| rustix | 1.1.4 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | fno-math, trading-core |  |
| rustversion | 1.0.22 | MIT OR Apache-2.0 | fno-math |  |
| rusty-fork | 0.3.1 | MIT/Apache-2.0 | fno-math, trading-core |  |
| semver | 1.0.28 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| serde | 1.0.228 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| serde_core | 1.0.228 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| serde_derive | 1.0.228 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| serde_json | 1.0.150 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| serde_spanned | 1.1.1 | MIT OR Apache-2.0 | fno-math |  |
| shlex | 2.0.1 | MIT OR Apache-2.0 | fno-math |  |
| simdutf8 | 0.1.5 | MIT OR Apache-2.0 | fno-math |  |
| siphasher | 1.0.3 | MIT/Apache-2.0 | fno-math |  |
| slab | 0.4.12 | MIT | fno-math |  |
| syn | 2.0.117 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| target-lexicon | 0.13.5 | Apache-2.0 WITH LLVM-exception | fno-math |  |
| tempfile | 3.27.0 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| thiserror | 2.0.18 | MIT OR Apache-2.0 | fno-math |  |
| thiserror-impl | 2.0.18 | MIT OR Apache-2.0 | fno-math |  |
| tinyvec | 1.11.0 | Zlib OR Apache-2.0 OR MIT | fno-math |  |
| tinyvec_macros | 0.1.1 | MIT OR Apache-2.0 OR Zlib | fno-math |  |
| toml | 1.1.2+spec-1.1.0 | MIT OR Apache-2.0 | fno-math |  |
| toml_datetime | 1.1.1+spec-1.1.0 | MIT OR Apache-2.0 | fno-math |  |
| toml_parser | 1.1.2+spec-1.1.0 | MIT OR Apache-2.0 | fno-math |  |
| toml_writer | 1.1.1+spec-1.1.0 | MIT OR Apache-2.0 | fno-math |  |
| tracing | 0.1.44 | MIT | fno-math |  |
| tracing-attributes | 0.1.31 | MIT | fno-math |  |
| tracing-core | 0.1.36 | MIT | fno-math |  |
| trading-core | 0.3.0 | Proprietary | trading-core | PROJECT |
| unarray | 0.1.4 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| unicode-ident | 1.0.24 | (MIT OR Apache-2.0) AND Unicode-3.0 | fno-math, trading-core |  |
| unicode-xid | 0.2.6 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| uuid | 1.23.2 | Apache-2.0 OR MIT | fno-math |  |
| wait-timeout | 0.2.1 | MIT/Apache-2.0 | fno-math, trading-core |  |
| wasip2 | 1.0.3+wasi-0.2.9 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | fno-math, trading-core |  |
| wasip3 | 0.4.0+wasi-0.3.0-rc-2026-01-06 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | fno-math, trading-core |  |
| wasm-bindgen | 0.2.122 | MIT OR Apache-2.0 | fno-math |  |
| wasm-bindgen-macro | 0.2.122 | MIT OR Apache-2.0 | fno-math |  |
| wasm-bindgen-macro-support | 0.2.122 | MIT OR Apache-2.0 | fno-math |  |
| wasm-bindgen-shared | 0.2.122 | MIT OR Apache-2.0 | fno-math |  |
| wasm-encoder | 0.244.0 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | fno-math, trading-core |  |
| wasm-metadata | 0.244.0 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | fno-math, trading-core |  |
| wasmparser | 0.244.0 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | fno-math, trading-core |  |
| windows-core | 0.62.2 | MIT OR Apache-2.0 | fno-math |  |
| windows-implement | 0.60.2 | MIT OR Apache-2.0 | fno-math |  |
| windows-interface | 0.59.3 | MIT OR Apache-2.0 | fno-math |  |
| windows-link | 0.2.1 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| windows-result | 0.4.1 | MIT OR Apache-2.0 | fno-math |  |
| windows-strings | 0.5.1 | MIT OR Apache-2.0 | fno-math |  |
| windows-sys | 0.61.2 | MIT OR Apache-2.0 | fno-math, trading-core |  |
| winnow | 1.0.3 | MIT | fno-math |  |
| wit-bindgen | 0.51.0 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | fno-math, trading-core |  |
| wit-bindgen | 0.57.1 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | fno-math, trading-core |  |
| wit-bindgen-core | 0.51.0 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | fno-math, trading-core |  |
| wit-bindgen-rust | 0.51.0 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | fno-math, trading-core |  |
| wit-bindgen-rust-macro | 0.51.0 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | fno-math, trading-core |  |
| wit-component | 0.244.0 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | fno-math, trading-core |  |
| wit-parser | 0.244.0 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | fno-math, trading-core |  |
| zerocopy | 0.8.50 | BSD-2-Clause OR Apache-2.0 OR MIT | fno-math, trading-core |  |
| zerocopy-derive | 0.8.50 | BSD-2-Clause OR Apache-2.0 OR MIT | fno-math, trading-core |  |
| zmij | 1.0.21 | MIT | fno-math, trading-core |  |

## Flag summary

- Python: 0 strong-copyleft, 0 weak-copyleft (sole-license), 0 unknown/missing.
- Rust: 0 strong-copyleft, 0 weak-copyleft (sole-license), 0 unknown/missing.
- File-level weak-copyleft (MPL-2.0) and notice-only licenses present — see posture report §2 for the obligations.

