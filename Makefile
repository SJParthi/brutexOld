# BruteX — development Makefile
# Every target maps directly to a CI gate in REQUIREMENTS.md §17.

.PHONY: help install fmt lint type test test-unit test-cov audit bench clean all

help:
	@echo "BruteX development targets:"
	@echo "  install    — install package + dev dependencies via pip"
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

install:
	pip install -e ".[dev]"

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
