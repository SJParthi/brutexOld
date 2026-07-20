# BruteX — development Makefile
# Every target maps directly to a CI gate in REQUIREMENTS.md §17.

.PHONY: help setup install run run-raw preflight fmt lint type test test-unit test-cov audit bench clean wipe wipe-dry all

help:
	@echo "BruteX development targets:"
	@echo "  setup      — turnkey full env: venv + all libs + Rust kernel (scripts/setup/bootstrap.sh)"
	@echo "  install    — install package + dev dependencies via pip (into the active env)"
	@echo "  run        — ONE entry: delegates to ./up.sh (self-check → self-heal → dashboard; ~1s fast path when already ready) — same path as the IDE ▶ BruteX config"
	@echo "  run-raw    — RAW direct entry (pre-unification run body): venv + [data,ui,groww] libs + kernels + python -m brutex run, NO up.sh self-checks (CI/debugging)"
	@echo "  fmt        — run ruff format"
	@echo "  lint       — run ruff check"
	@echo "  type       — run mypy --strict + pyright"
	@echo "  test       — run full pytest suite"
	@echo "  test-unit  — run only unit tests (fast)"
	@echo "  test-cov   — run pytest with coverage gate (fail under 100%)"
	@echo "  audit      — run bandit + pip-audit security scans"
	@echo "  bench      — run pytest-benchmark suite"
	@echo "  all        — fmt + lint + type + test-cov + audit"
	@echo "  clean      — remove caches and build artifacts"

# Turnkey env vars (DEC-CLONE-RUN-002). `run`/`setup` are self-contained:
# they create the venv and pull every needed library so a clean clone
# "just runs". Override the interpreter with `make run PY=python3.12`.
PY   ?= python3.12
VENV ?= .venv
VPY  := $(VENV)/bin/python
VPIP := $(VENV)/bin/pip

# Log-redirect knob (DEC-INSTRUMENT-UNIVERSE-FAST-001 + DEC-LEAN-REINGEST-FIX-001).
# Scoped to the RAW direct entry + the wipe targets ONLY (target-specific
# export, DEC-ONE-ENTRY-UNIFY-001): the unified `run` → ./up.sh path uses the
# runtime default ~/.brutex/logs/brutex.log, so `make run`, `./up.sh`, and the
# IDE ▶ BruteX config are byte-identical (ONE log location, printed at boot).
# `make run-raw` keeps the in-repo ./.brutex-logs default so an IDE can tail
# it; override with `make run-raw LOG_DIR=/some/other/path`, or
# `make run-raw LOG_DIR=` to fall back to the runtime's ~/.brutex/logs
# default. ./.brutex-logs/ is .gitignored.
LOG_DIR ?= ./.brutex-logs
ifneq ($(LOG_DIR),)
run-raw wipe wipe-dry: export BRUTEX_LOG_DIR := $(LOG_DIR)
endif

install:
	pip install -e ".[dev]"

# Turnkey full development environment: venv + [dev,data,ui] +
# Rust toolchain + fno_math kernel + doctor smoke. The single source of
# truth is scripts/setup/bootstrap.sh (CLAUDE.md §11 COMMON — no recipe
# copy-paste).
setup:
	bash scripts/setup/bootstrap.sh

# Verify the three host tools the raw entry depends on (python3.12 / make /
# cargo) BEFORE doing any work, so a missing tool yields a clear one-line fix
# instead of a cryptic downstream crash (DEC-RUN-PREFLIGHT-001).
preflight:
	@PY="$(PY)" bash scripts/run/preflight.sh

# ONE entry point (DEC-ONE-ENTRY-UNIFY-001): `make run`, `./up.sh`, and the
# committed IntelliJ ▶ "BruteX" run configuration all execute the SAME path —
# ./up.sh (self-check → self-heal → start dashboard, DEC-ONE-COMMAND-UP-001).
# up.sh fast-paths to "already ready" in about a second when a dashboard is
# already running, so repeat runs stay instant. The operator never chooses
# between entries. The pre-unification direct body is preserved verbatim as
# `run-raw` below (CI reproduction / debugging). NOTE: the CI "make run smoke
# (cold-start boot)" job does NOT invoke make — it reproduces the run-raw
# dependency set step-by-step in .github/workflows/ci.yml, so re-wiring `run`
# cannot break it (asserted by tests/deploy/test_one_entry_unify.py).
run:
	./up.sh

# run-raw — the RAW direct entry (the pre-DEC-ONE-ENTRY-UNIFY-001 `run` body,
# DEC-CLONE-RUN-002 + DEC-ONECLICK-RUN-001): creates the venv, installs the
# dashboard runtime + Groww live libs ([data,ui,groww] — Groww authenticates
# via the growwapi SDK + pyotp TOTP, so the live probe/pull need the [groww]
# extra; DEC-DHAN-GROWW-REMIGRATE-001 / DEC-MAKE-RUN-GROWW-EXTRA-001), builds
# the fno_math Rust kernel when it is not importable (brutex.strategies.combo
# imports it at module top, combo.py:64) AND the trading_core kernel (the
# shared 52-vocab core — brutex.indicators.fib_levels imports it at module
# top, fib_levels.py:68, DEC-SHARED-CORE-001), then launches the one-click
# `brutex run` (resolves the lake root, opens the browser, boots the server).
# Idempotent: re-running skips work already done. Open http://127.0.0.1:9080.
# NO up.sh self-check/self-heal chain — use `make run` (or ./up.sh) for that.
run-raw: preflight
	@test -d $(VENV) || $(PY) -m venv $(VENV)
	$(VPIP) install --upgrade pip
	$(VPIP) install -e ".[data,ui,groww]"
	@$(VPY) -c "import fno_math" 2>/dev/null || { \
		echo "==> fno_math kernel not importable — building it (one-time; needs cargo)"; \
		$(VPIP) install maturin; \
		cd rust/fno-math && PATH="$$HOME/.cargo/bin:$$PATH" VIRTUAL_ENV="$(CURDIR)/$(VENV)" "$(CURDIR)/$(VENV)/bin/maturin" develop --release; \
	}
	@$(VPY) -c "import trading_core" 2>/dev/null || { \
		echo "==> trading_core kernel not importable — building it (one-time; needs cargo)"; \
		$(VPIP) install maturin; \
		cd rust/trading-core && PATH="$$HOME/.cargo/bin:$$PATH" VIRTUAL_ENV="$(CURDIR)/$(VENV)" "$(CURDIR)/$(VENV)/bin/maturin" develop --release; \
	}
	@$(VPY) -c "import brutex_feed" 2>/dev/null || { \
		echo "==> brutex_feed kernel not importable — building it (optional; DEC-FEED-RUST-CORE-001)"; \
		$(VPIP) install maturin; \
		{ cd rust/brutex-feed && PATH="$$HOME/.cargo/bin:$$PATH" VIRTUAL_ENV="$(CURDIR)/$(VENV)" "$(CURDIR)/$(VENV)/bin/maturin" develop --release; } \
			|| echo "==> brutex_feed build failed — OPTIONAL kernel (BRUTEX_FEED_RUST is default-OFF; the feed degrades to the Python oracle)"; \
	}
	@$(VPY) -c "import brutex_pull_state" 2>/dev/null || { \
		echo "==> brutex_pull_state kernel not importable — building it (REQUIRED; the pull-state decision plane is Rust-only and fail-closed)"; \
		$(VPIP) install maturin; \
		cd rust/brutex-pull-state && PATH="$$HOME/.cargo/bin:$$PATH" VIRTUAL_ENV="$(CURDIR)/$(VENV)" "$(CURDIR)/$(VENV)/bin/maturin" develop --release; \
	}
	$(VPY) -m brutex run

fmt:
	ruff format brutex tests

lint:
	ruff check brutex tests

type:
	mypy --strict brutex
	pyright brutex

test:
	pytest

test-unit:
	pytest tests/unit -x

test-cov:
	pytest --cov=brutex --cov-branch --cov-report=term-missing --cov-fail-under=100

audit:
	bandit -r brutex -ll
	pip-audit

bench:
	pytest tests/bench --benchmark-only

all: fmt lint type test-cov audit
	@echo "✅ All CG gates passed"

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov build dist *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# Total-wipe of ALL persisted BruteX data — the lake (instruments / expiries /
# contracts), saved credentials, cached token, audit log, and run logs under
# ~/.brutex (+ any BRUTEX_* override). This is what deleting the repo folder
# does NOT do (data lives in the home dir, not the repo). `make wipe-dry`
# PREVIEWS the plan; `make wipe` deletes after a typed-WIPE confirmation
# (refuses while a dashboard is running). DEC-DATA-WIPE-001.
wipe-dry:
	$(VPY) -m brutex wipe

wipe:
	$(VPY) -m brutex wipe --execute
