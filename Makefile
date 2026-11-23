# SPDX-License-Identifier: GPL-3.0-only
# Gates de qualidade (plan 10.1 e 10.6). Alvos de front delegam a `pnpm --dir frontend`;
# a suíte E2E (Playwright) é um projeto pnpm próprio em `e2e/` (plan 10.5).

.PHONY: lint lint-python lint-frontend lint-e2e lint-repo deps-audit license-check test test-python \
	test-frontend build-frontend build-dist verify-dist sbom sbom-python sbom-frontend contract test-integration test-resilience test-telemetry model-data \
	test-model-unit test-model test-volume test-e2e test-all obs-up obs-down

FRONTEND_DIR := frontend
E2E_DIR := e2e
# pytest sem testes coletados retorna 5; só é tolerado nas suítes vazias marcadas.
PYTEST_EMPTY_OK = rc=$$?; [ $$rc -eq 5 ] && echo "suite vazia (exit 5 tolerado)" || exit $$rc

# Relatórios de lint (JSON) para o Sonar (TF-143-06) ficam em build/reports/ (não versionado);
# a execução com saída humana vem primeiro, então o JSON só é gravado com o lint limpo.
REPORTS_DIR := build/reports

lint-python:
	uv run ruff check .
	mkdir -p $(REPORTS_DIR)
	uv run ruff check . --output-format json --output-file $(REPORTS_DIR)/ruff.json
	uv run ruff format --check .
	uv run mypy
	uv run lint-imports
	uv run deptry .
	uv run vulture

lint-frontend:
	pnpm --dir $(FRONTEND_DIR) install --frozen-lockfile
	pnpm --dir $(FRONTEND_DIR) run lint
	mkdir -p $(REPORTS_DIR)
	pnpm --dir $(FRONTEND_DIR) run lint:report

lint-e2e:
	pnpm --dir $(E2E_DIR) install --frozen-lockfile
	pnpm --dir $(E2E_DIR) run lint

build-frontend:
	pnpm --dir $(FRONTEND_DIR) install --frozen-lockfile
	pnpm --dir $(FRONTEND_DIR) run build

# Pacote de release (plan 11.6, TF-143-07): SPA compilada e embutida no wheel + sdist em dist/.
DIST_DIR := dist

build-dist: build-frontend
	rm -rf $(DIST_DIR)
	uv build --out-dir $(DIST_DIR)

# Reprova wheel/sdist com conteúdo fora da allowlist (segredos, bancos, tests/, imagens fora de
# assets, SPA ausente). VERSION=X.Y.Z também confere a versão dos nomes dos arquivos.
verify-dist:
	uv run --no-project python scripts/verify_dist.py $(DIST_DIR) $(if $(VERSION),--version $(VERSION))

# SBOM CycloneDX 1.6 (plan 11.6). Python: dependências de runtime do uv.lock; as entradas `+cpu`
# do índice CPU-only do Linux saem, para listar o torch do PyPI usado no macOS (alvo do produto).
# Frontend: `pnpm sbom` (nativo, lê o pnpm-lock.yaml): `@cyclonedx/cyclonedx-npm` depende de
# `npm ls` e não lê a árvore do pnpm (inclui devDependencies e falha com peers ausentes).
SBOM_DIR := build/sbom

sbom: sbom-python sbom-frontend

sbom-python:
	mkdir -p $(SBOM_DIR)
	uv export --locked --no-dev --no-emit-project --no-hashes --format requirements-txt \
		| grep -v -- '+cpu' | sed "s/ ; sys_platform != 'linux'//" > $(SBOM_DIR)/requirements.txt
	uv run --locked --only-group release cyclonedx-py requirements --pyproject pyproject.toml \
		--mc-type application --spec-version 1.6 --output-reproducible --output-format JSON \
		--output-file $(SBOM_DIR)/sbom-python.cdx.json $(SBOM_DIR)/requirements.txt

sbom-frontend:
	mkdir -p $(SBOM_DIR)
	pnpm --dir $(FRONTEND_DIR) install --frozen-lockfile
	pnpm --dir $(FRONTEND_DIR) sbom --sbom-format cyclonedx --sbom-spec-version 1.6 \
		--sbom-type application --prod --out $(CURDIR)/$(SBOM_DIR)/sbom-frontend.cdx.json

# Contrato OpenAPI -> tipos TS (plan 4.1): regenera e falha se divergir do versionado.
contract:
	uv run python scripts/export_openapi.py
	pnpm --dir $(FRONTEND_DIR) install --frozen-lockfile
	pnpm --dir $(FRONTEND_DIR) run contract:types
	git diff --exit-code -- $(FRONTEND_DIR)/src/api/openapi.json $(FRONTEND_DIR)/src/api/schema.gen.ts

# Verificações do repositório (plan 10.6): workflows, YAML, Markdown, TOML, SPDX e PlantUML.
# zizmor usa GH_TOKEN/GITHUB_TOKEN nas auditorias online (pins, actions vulneráveis); sem token
# elas são puladas localmente e rodam no CI. PLANTUML aponta para o jar no CI.
PLANTUML ?= plantuml
MARKDOWNLINT := pnpm dlx markdownlint-cli2@0.24.1

lint-repo:
	uv run actionlint
	uv run zizmor --no-progress .github
	uv run yamllint --strict .
	$(MARKDOWNLINT)
	uv run taplo check
	uv run taplo format --check
	uv run python scripts/check_spdx.py
	uv run python scripts/check_test_pairs.py
	$(PLANTUML) -checkonly -failonerror "docs/**/*.md"

# Vulnerabilidades conhecidas nas dependências resolvidas: Python (uv.lock via pip-audit; o sufixo
# local `+cpu` do torch do índice CPU-only é removido para consulta no PyPI) e npm (nível alto+).
AUDIT_DIR := build/audit

deps-audit:
	mkdir -p $(AUDIT_DIR)
	uv export --locked --no-emit-project --no-hashes --format requirements-txt \
		| sed 's/+cpu//' > $(AUDIT_DIR)/requirements.txt
	uv run pip-audit -r $(AUDIT_DIR)/requirements.txt --no-deps --disable-pip --progress-spinner off
	pnpm --dir $(FRONTEND_DIR) install --frozen-lockfile
	pnpm --dir $(FRONTEND_DIR) audit --audit-level high
	pnpm --dir $(E2E_DIR) install --frozen-lockfile
	pnpm --dir $(E2E_DIR) audit --audit-level high

# Licenças compatíveis com GPL-3.0 (plan 11.5): Python instalado (runtime + dev) e npm de produção
# (o que a SPA embute). Exceções verificadas: [tool.odc.license-check.overrides] no pyproject.
LICENSE_DIR := build/license-check

license-check:
	mkdir -p $(LICENSE_DIR)
	uv run pip-licenses --from=mixed --format=json --output-file $(LICENSE_DIR)/python.json
	pnpm --dir $(FRONTEND_DIR) install --frozen-lockfile
	pnpm --dir $(FRONTEND_DIR) licenses list --json --prod > $(LICENSE_DIR)/node.json
	uv run python scripts/license_check.py --python $(LICENSE_DIR)/python.json --node $(LICENSE_DIR)/node.json

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

# Testes rápidos do modelo para o PR (sem `model`/`perf`); suíte vazia (exit 5) é tolerada.
test-model-unit:
	uv run pytest tests/model -m "not model and not perf" --no-cov -q; $(PYTEST_EMPTY_OK)

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
