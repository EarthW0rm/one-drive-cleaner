# SPDX-License-Identifier: GPL-3.0-only
# Gates de qualidade (plan 10.1 e 10.6). Alvos de front delegam a `pnpm --dir frontend`;
# enquanto `frontend/` não existe (até a TF-143-03) viram no-op com aviso.

.PHONY: lint lint-python lint-frontend lint-repo test test-python test-frontend \
	test-integration test-model test-e2e test-all

FRONTEND_DIR := frontend
# pytest sem testes coletados retorna 5; só é tolerado nas suítes vazias marcadas.
PYTEST_EMPTY_OK = rc=$$?; [ $$rc -eq 5 ] && echo "suite vazia (exit 5 tolerado)" || exit $$rc

lint-python:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run lint-imports
	uv run deptry .
	uv run vulture

lint-frontend:
	@if [ -f $(FRONTEND_DIR)/package.json ]; then pnpm --dir $(FRONTEND_DIR) run lint; \
	else echo "AVISO: frontend/ inexistente; lint-frontend ignorado (TF-143-03)"; fi

lint-repo:
	uv run python scripts/check_spdx.py
	uv run python scripts/check_test_pairs.py

lint: lint-python lint-frontend lint-repo

test-python:
	uv run pytest tests/unit

test-frontend:
	@if [ -f $(FRONTEND_DIR)/package.json ]; then pnpm --dir $(FRONTEND_DIR) run test; \
	else echo "AVISO: frontend/ inexistente; test-frontend ignorado (TF-143-03)"; fi

test: test-python test-frontend

test-integration:
	uv run pytest -m integration --no-cov tests; $(PYTEST_EMPTY_OK)

test-model:
	uv run pytest -m "model or perf" --no-cov tests; $(PYTEST_EMPTY_OK)

test-e2e:
	@if [ -f $(FRONTEND_DIR)/package.json ] && [ -f e2e/playwright.config.ts ]; then \
		pnpm --dir $(FRONTEND_DIR) exec playwright test --config ../e2e/playwright.config.ts; \
	else echo "AVISO: e2e/ inexistente; test-e2e ignorado (TF-143-03)"; fi

test-all: lint test test-integration test-model test-e2e
