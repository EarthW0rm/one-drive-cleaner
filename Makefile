# SPDX-License-Identifier: GPL-3.0-only
# Gates de qualidade (plan 10.1 e 10.6). Alvos de front delegam a `pnpm --dir frontend`;
# `e2e/` ainda não existe (suíte E2E futura), então `test-e2e` é no-op com aviso até lá.

.PHONY: lint lint-python lint-frontend lint-repo test test-python test-frontend \
	build-frontend contract test-integration test-model test-e2e test-all obs-up obs-down

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
	pnpm --dir $(FRONTEND_DIR) install --frozen-lockfile
	pnpm --dir $(FRONTEND_DIR) run lint

build-frontend:
	pnpm --dir $(FRONTEND_DIR) install --frozen-lockfile
	pnpm --dir $(FRONTEND_DIR) run build

# Contrato OpenAPI -> tipos TS (plan 4.1): regenera e falha se divergir do versionado.
contract:
	uv run python scripts/export_openapi.py
	pnpm --dir $(FRONTEND_DIR) install --frozen-lockfile
	pnpm --dir $(FRONTEND_DIR) run contract:types
	git diff --exit-code -- $(FRONTEND_DIR)/src/api/openapi.json $(FRONTEND_DIR)/src/api/schema.gen.ts

lint-repo:
	uv run python scripts/check_spdx.py
	uv run python scripts/check_test_pairs.py

lint: lint-python lint-frontend lint-repo

test-python:
	uv run pytest tests/unit

test-frontend:
	pnpm --dir $(FRONTEND_DIR) install --frozen-lockfile
	pnpm --dir $(FRONTEND_DIR) run test

test: test-python test-frontend

test-integration:
	uv run pytest -m integration --no-cov tests; $(PYTEST_EMPTY_OK)

test-model:
	uv run pytest -m "model or perf" --no-cov tests; $(PYTEST_EMPTY_OK)

test-e2e:
	@if [ -f e2e/playwright.config.ts ]; then \
		pnpm --dir $(FRONTEND_DIR) exec playwright test --config ../e2e/playwright.config.ts; \
	else echo "AVISO: e2e/ inexistente; test-e2e ignorado (suíte E2E ainda não criada)"; fi

test-all: lint test test-integration test-model test-e2e

# Stack local opcional de monitoramento (Grafana em http://127.0.0.1:3000; OTLP em 127.0.0.1:4317).
OBS_COMPOSE := docker compose -f deploy/observability/docker-compose.yml

obs-up:
	$(OBS_COMPOSE) up -d --wait

obs-down:
	$(OBS_COMPOSE) down
