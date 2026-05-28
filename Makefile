# BruteX — development Makefile
# Every target maps directly to a CI gate in REQUIREMENTS.md §17.

.PHONY: help setup install run fmt lint type test test-unit test-cov audit bench clean all

help:
	@echo "BruteX development targets:"
	@echo "  setup      — turnkey full env: venv + all libs + Rust kernel (scripts/setup/bootstrap.sh)"
	@echo "  install    — install package + dev dependencies via pip (into the active env)"
	@echo "  run        — turnkey: venv + [data,ui] libs + build fno_math kernel + launch dashboard (http://127.0.0.1:9080)"
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

install:
	pip install -e ".[dev]"

# Turnkey full development environment: venv + [dev,data,ui,groww] +
# Rust toolchain + fno_math kernel + doctor smoke. The single source of
# truth is scripts/setup/bootstrap.sh (CLAUDE.md §11 COMMON — no recipe
# copy-paste).
setup:
	bash scripts/setup/bootstrap.sh

# One-command clone-and-run (DEC-CLONE-RUN-001 -> -002). Creates the venv,
# installs the dashboard runtime libs ([data,ui]), builds the fno_math Rust
# kernel when it is not importable (brutex.strategies.combo imports it at
# module top, combo.py:64), then launches the server. Idempotent: re-running
# skips work already done. Open http://127.0.0.1:9080.
run:
	@test -d $(VENV) || $(PY) -m venv $(VENV)
	$(VPIP) install --upgrade pip
	$(VPIP) install -e ".[data,ui]"
	@$(VPY) -c "import fno_math" 2>/dev/null || { \
		echo "==> fno_math kernel not importable — building it (one-time; needs cargo)"; \
		$(VPIP) install maturin; \
		cd rust/fno-math && PATH="$$HOME/.cargo/bin:$$PATH" VIRTUAL_ENV="$(CURDIR)/$(VENV)" "$(CURDIR)/$(VENV)/bin/maturin" develop --release; \
	}
	$(VPY) -m brutex dashboard

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
