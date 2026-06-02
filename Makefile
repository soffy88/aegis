# Aegis development commands
# 强制用 .venv 跑 (防 backlog-006 系统 pytest 漏装 testcontainers 类问题)
PYTHON := .venv/bin/python
PYTEST := $(PYTHON) -m pytest

.PHONY: help test test-smoke test-unit lint typecheck format check sync test-c3-e2e

help:
	@echo "Aegis development commands:"
	@echo "  make sync       - uv sync (装 deps)"
	@echo "  make test       - 跑所有测试 (含 testcontainers smoke)"
	@echo "  make test-unit  - 跑 unit 测试 (跳 testcontainers smoke)"
	@echo "  make test-smoke - 仅跑 smoke 测试"
	@echo "  make lint       - ruff lint"
	@echo "  make typecheck  - mypy typecheck"
	@echo "  make format     - ruff format"
	@echo "  make check      - lint + typecheck + test (CI 跑这个)"
	@echo "  make test-c3-e2e - C3-7 e2e: 触发测试错误 + DB 验证全链路 (需 AEGIS_SENTRY_DSN + Aegis local server)"

sync:
	uv sync --all-extras

test:
	RUN_SMOKE=1 $(PYTEST) aegis/tests/

test-unit:
	RUN_SMOKE=0 $(PYTEST) aegis/tests/ --ignore=aegis/tests/smoke/

test-smoke:
	RUN_SMOKE=1 $(PYTEST) aegis/tests/smoke/

lint:
	$(PYTHON) -m ruff check aegis/

typecheck:
	$(PYTHON) -m mypy aegis/

format:
	$(PYTHON) -m ruff format aegis/

check: lint typecheck test

test-c3-e2e: ## C3-7 e2e: 触发测试错误 + DB 验证全链路
	AEGIS_SENTRY_ENABLED=true \
	AEGIS_SENTRY_DSN=$(AEGIS_SENTRY_DSN) \
	$(PYTHON) -m scripts.c3_e2e_trigger
