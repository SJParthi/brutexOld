# BruteX — development Makefile
# Every target maps directly to a CI gate in REQUIREMENTS.md §17.

.PHONY: help setup install run preflight fmt lint type test test-unit test-cov audit bench clean wipe wipe-dry all

help:
	@echo "BruteX development targets:"
	@echo "  setup      — turnkey full env: venv + all libs + Rust kernel (scripts/setup/bootstrap.sh)"
	@echo "  install    — install package + dev dependencies via pip (into the active env)"
	@echo "  run        — turnkey one-click: venv + [data,ui] libs + fno_math kernel + launch (opens browser, http://127.0.0.1:9080)"
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
# Defaults to ./.brutex-logs (in the repo tree) so `make run` logs are visible
# in IntelliJ/IDEA out of the box (and the /logs web viewer reads the same
# file). Override with `make run LOG_DIR=/some/other/path`, or
# `make run LOG_DIR=` to fall back to the runtime's ~/.brutex/logs default.
# ./.brutex-logs/ is .gitignored.
LOG_DIR ?= ./.brutex-logs
ifneq ($(LOG_DIR),)
export BRUTEX_LOG_DIR := $(LOG_DIR)
endif

install:
	pip install -e ".[dev]"

# Turnkey full development environment: venv + [dev,data,ui] +
# Rust toolchain + fno_math kernel + doctor smoke. The single source of
# truth is scripts/setup/bootstrap.sh (CLAUDE.md §11 COMMON — no recipe
# copy-paste).
setup:
	bash scripts/setup/bootstrap.sh

# One-command clone-and-run (DEC-CLONE-RUN-002 + DEC-ONECLICK-RUN-001). Creates
# the venv, installs the dashboard runtime libs ([data,ui] — Dhan needs no
# vendor SDK; auth is a manual access-token + client-id over plain REST,
# DEC-DHAN-FINALIZE-001), builds the fno_math
# Rust kernel when it is not importable (brutex.strategies.combo imports it at
# module top, combo.py:64), then launches the one-click `brutex run` (resolves
# the lake root, opens the browser, boots the server). Idempotent: re-running
# skips work already done. Open http://127.0.0.1:9080.
# Verify the three host tools `run` depends on (python3.12 / make / cargo)
# BEFORE doing any work, so a missing tool yields a clear one-line fix instead
# of a cryptic downstream crash (DEC-RUN-PREFLIGHT-001).
preflight:
	@PY="$(PY)" bash scripts/run/preflight.sh

run: preflight
	@test -d $(VENV) || $(PY) -m venv $(VENV)
	$(VPIP) install --upgrade pip
	$(VPIP) install -e ".[data,ui]"
	@$(VPY) -c "import fno_math" 2>/dev/null || { \
		echo "==> fno_math kernel not importable — building it (one-time; needs cargo)"; \
		$(VPIP) install maturin; \
		cd rust/fno-math && PATH="$$HOME/.cargo/bin:$$PATH" VIRTUAL_ENV="$(CURDIR)/$(VENV)" "$(CURDIR)/$(VENV)/bin/maturin" develop --release; \
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
