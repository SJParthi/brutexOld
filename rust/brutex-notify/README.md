# brutex-notify

The notification PURE-law kernel (DEC-NOTIFY-RUST-CORE-001): the four pure
notification laws of `brutex/notifications/` — the MarkdownV2 escape law,
the `_count_or_question` honest-unknown law, the dedupe-window drop
predicate (over IEEE-754 f64 bit patterns as `u64` — no float object ever
crosses the FFI), and the 12-code-point id truncation — transcribed
token-for-token from their Python oracles.

Consumed ONLY through the default-OFF `BRUTEX_NOTIFY_RUST` facade
(`brutex/notifications/notify_rust.py`), which retains the verbatim Python
law bodies as the LIVE parity oracle (16-call per-law burn-in bit-compare +
ship-the-oracle autorevert). Everything else in the notification chain — the
~30 f-string message builders (named follow-up DEC-NOTIFY-RUST-BUILDERS-002),
the `TelegramNotifier` HTTP transport (network I/O), threads/queues/atexit,
the wiring singleton — STAYS Python, disclosed honestly.

Honest envelope (Golden Rule #10 / the ADR-0020 banner): expected payoff
≈ 0 — conversion is for uniformity, not speed. The Telegram send is
network-bound I/O and is untouched.

Build (wheel): `maturin develop --release` (or `pip install .`) inside this
directory with a Python ≥ 3.12 venv active. Test (pure core, no libpython):
`cargo test --release`.
