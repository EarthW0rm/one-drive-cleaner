# SPDX-License-Identifier: GPL-3.0-only
# Gates de qualidade (plan 10.1 e 10.6). Alvos de front delegam a `pnpm --dir frontend`;
# a suíte E2E (Playwright) é um projeto pnpm próprio em `e2e/` (plan 10.5).

.PHONY: lint lint-python lint-frontend lint-e2e lint-repo test test-python test-frontend \
	build-frontend contract test-integration test-resilience test-telemetry model-data test-model test-volume test-e2e test-all obs-up obs-down

FRONTEND_DIR := frontend
E2E_DIR := e2e
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

lint-e2e:
	pnpm --dir $(E2E_DIR) install --frozen-lockfile
	pnpm --dir $(E2E_DIR) run lint

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

lint: lint-python lint-frontend lint-e2e lint-repo

test-python:
	uv run pytest tests/unit

test-frontend:
	pnpm --dir $(FRONTEND_DIR) install --frozen-lockfile
	pnpm --dir $(FRONTEND_DIR) run test

test: test-python test-frontend

test-integration:
	uv run pytest -m integration --no-cov tests; $(PYTEST_EMPTY_OK)

# Kill-and-resume (plan 10.2, TF-157-03): `odc` real em subprocesso morto com SIGKILL em varredura,
# extração, movimentação e restauração. Já faz parte de `test-integration` (mesmo marcador).
test-resilience:
	uv run pytest tests/resilience -m resilience --no-cov; $(PYTEST_EMPTY_OK)

# Privacidade da telemetria ponta a ponta (RN-24, TF-157-05): jornada completa do `odc` com
# exportadores em memória; falha se nomes, caminhos, ids, tokens, URLs ou e-mail aparecerem.
# Já faz parte de `test-integration` (mesmo marcador).
test-telemetry:
	uv run pytest tests/telemetry -m integration --no-cov

# Conjunto de referência CC0 do modelo (plan 10.3): baixa para tests/model/data/ (não versionado)
# e verifica o SHA-256 de cada arquivo. Sem ele, os testes `model` de qualidade são pulados.
model-data:
	uv run python tests/model/data_gen/download.py

test-model:
	uv run pytest -m "model or perf" --no-cov tests; $(PYTEST_EMPTY_OK)

# Volumetria (plan 10.2, TF-157-04): 50 mil itens no `odc` real com fakes; mede os SLAs da spec 5.2
# e grava volume-report.json (ODC_VOLUME_REPORT muda o caminho). Semanal/manual: fora de `test-all`.
test-volume:
	uv run pytest tests/volume -m volume --no-cov -q

# E2E (plan 10.5): Chromium e WebKit contra o `odc` real com Graph, identidade e embedder fakes
# (launcher em tests/e2e_launcher). A SPA é recompilada antes; os navegadores são instalados
# sob demanda (idempotente). Sem conta real, sem fotos reais.
test-e2e: build-frontend
	pnpm --dir $(E2E_DIR) install --frozen-lockfile
	pnpm --dir $(E2E_DIR) run browsers
	pnpm --dir $(E2E_DIR) run test

test-all: lint test test-integration test-model test-e2e

# Stack local opcional de monitoramento (Grafana em http://127.0.0.1:3000; OTLP em 127.0.0.1:4317).
OBS_COMPOSE := docker compose -f deploy/observability/docker-compose.yml

obs-up:
	$(OBS_COMPOSE) up -d --wait

obs-down:
	$(OBS_COMPOSE) down
