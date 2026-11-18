# Design: OneDrive Cleaner

- **Data:** 2026-11-17
- **Status:** Aprovado (brainstorming) — insumo técnico para `docs/specs/onedrive-cleaner/plan.md`
- **Spec funcional:** [`docs/specs/onedrive-cleaner/spec.md`](../specs/onedrive-cleaner/spec.md)

## 1. Contexto / Problema

O Camera Roll do OneDrive pessoal acumula prints de tela, imagens recebidas pelo WhatsApp, memes e documentos fotografados misturados a fotos reais (família, animais, viagens). A limpeza manual é inviável com milhares de itens. Imagens vindas do WhatsApp não podem ser descartadas por origem: parte delas são fotos de família enviadas por terceiros.

Objetivo: aplicação Python local que identifica imagens "não-foto" no OneDrive do utilizador, aprende com as classificações dele e move os itens para uma quarentena reversível, com exclusão opcional e explícita.

## 2. Abordagem

**Escolhida: A — embeddings locais + metadados + classificador leve (MVP); B — fine-tuning (evolução futura).**

| Critério | A — Embeddings + classificador leve | B — Fine-tuning de CNN |
|---|---|---|
| Retreino (M3, estimativa) | 1–5 s (5 mil); 10–30 s (50 mil) | 10–25 min por ciclo |
| Necessidade das imagens no retreino | Não (usa vetores persistidos) | Sim (re-download ou cache) |
| Nova categoria | Rótulos ou prompt textual, sem reprocessar | Retreino da rede |
| Rótulos necessários | Dezenas por categoria | Centenas+, risco de overfitting |
| Disco final | Vetores ~10 MB (5 mil) / ~100 MB float32 (50 mil) | Checkpoint 20–350 MB + modelo base |

Os rótulos persistidos servem de dataset para B, caso a precisão de A estagne abaixo da meta.

**Modelo de visão:** definido por spike de benchmark (CLIP ViT-B/32 ~350 MB vs SigLIP so400m ~1,6 GB) em ~300 imagens rotuladas, medindo precisão, tempo para 5 mil imagens no M3 e disco. O spike define também o tamanho de miniatura usado para embedding.

Estimativas (não medidas) para 5 mil imagens em M3 base 16 GB: download de miniaturas 3–8 min; embeddings 1–2 min (ViT-B/32) ou 4–8 min (SigLIP).

## 3. Arquitetura e componentes

Processo Python 3.12+ único, local. Comando `odc` sobe servidor em `127.0.0.1` e abre o browser.

| # | Componente | Responsabilidade |
|---|---|---|
| 1 | Auth | MSAL Python, public client, PKCE, redirect localhost, tenant `consumers`. Scopes `Files.ReadWrite offline_access`. Token cache no Keychain (msal-extensions) |
| 2 | Graph Client | httpx; retry em 429/503 respeitando `Retry-After`, backoff exponencial com jitter (máx. 5 tentativas), concorrência adaptativa (AIMD), timeout explícito por request |
| 3 | Scanner | Delta query por pasta selecionada (padrão Camera Roll); upsert de metadados (facets `image`, `photo`, `file`); checkpoint = `deltaLink`; ignora pastas do app |
| 4 | Extrator de features | Miniatura → diretório temporário → embedding → persistência → remoção imediata do arquivo. Features de metadados: padrão de nome (`Screenshot_`, `-WA`), presença de cameraMake/Model, aspect ratio vs proporções de tela conhecidas, MIME/extensão, tamanho, localização presente |
| 5 | Classificador | Zero-shot (prompts por categoria) até haver rótulos suficientes; depois regressão logística (embedding + metadados). Versionamento, avaliação em holdout, ordenação por incerteza (active learning) |
| 6 | Job Runner | Worker em background no processo; fila persistida em SQLite; checkpoint e retomada |
| 7 | Quarentena | Move para `/OneDrive Cleaner/Quarentena` (`conflictBehavior=rename`), guarda parent/nome originais; restaura; exclui (lixeira do OneDrive) com confirmação; diário intenção→resultado |
| 8 | Web UI | SPA React + Ant Design 6 (revisado em 2026-11-17; substitui Jinja2 + HTMX), servida pelo FastAPI. Telas: Revisão, Quarentena, Categorias, Varreduras, Configurações, Métricas. Miniaturas exibidas via URLs pré-autenticadas da Graph direto no browser (sem disco) |
| 9 | Persistência | SQLite (WAL). Vetores como BLOB float16 |
| 10 | Limpeza | Purga do temporário ao fim do job e no startup; política de pesos do modelo; teto de temporário (padrão 2 GB) |
| 11 | Telemetria | OpenTelemetry SDK: traces, métricas e logs correlacionados, exportados via OTLP para backend local (ver seção 7) |

## 4. Fluxo de dados

Estados do item: `DESCOBERTO → ANALISADO → SUGERIDO → QUARENTENA → (RESTAURADO | EXCLUIDO)`; laterais `FALHA_ANALISE` e `REMOVIDO_EXTERNAMENTE`. Rótulo do utilizador é independente do estado.

| Fluxo | Resumo |
|---|---|
| F1 Varredura | Delta por pasta → upsert `DESCOBERTO` → `deltaLink` como checkpoint → enfileira extração em lotes (ex.: 64) |
| F2 Extração | Miniatura → embedding + features → commit atômico do lote → remove arquivo → `ANALISADO` |
| F3 Classificação | Modelo vigente sobre `ANALISADO` → categoria, confiança, versão → `SUGERIDO` |
| F4 Revisão | UI ordenada por incerteza; confirmação/correção vira rótulo; retreino a cada N rótulos ou sob demanda; reclassificação total |
| F5 Quarentena | Apenas categorias removíveis; "Foto real" protegida; modo Manual/Automático/Evolutivo; Graph move; `QUARENTENA` |
| F6 Restaurar | Falso positivo → rótulo de correção → volta à origem (recria caminho; fallback `/OneDrive Cleaner/Restaurados`) → `RESTAURADO` |
| F7 Excluir | Somente da quarentena, confirmação explícita → DELETE (lixeira) → `EXCLUIDO`; metadados e diário mantidos |
| F8 Mudanças externas | Delta detecta remoções/movimentos fora do app → estados atualizados |
| F9 Limpeza | Fim de job e startup |

Modos de automação: **Manual**, **Automático** (limiar de confiança padrão 0,98, configurável) e **Evolutivo** (padrão: Automático liberado com precisão ≥ 98% nas categorias removíveis e ≥ 200 rótulos do utilizador; volta a Manual se a precisão cair).

Após cada retreino, itens em quarentena reclassificados para categoria não removível são restaurados automaticamente (qualquer modo; `reason=reclassified`).

Uma conta pessoal ativa por instalação; registros isolados por conta. Disco final ≤ 500 MB, sem contar os pesos do modelo de visão.

## 5. Tratamento de erros

| Falha | Tratamento |
|---|---|
| 429/503 Graph | `Retry-After` + backoff com jitter; concorrência cai pela metade a cada 429 e sobe gradualmente |
| Token expirado/revogado | Refresh silencioso; falha → pausa jobs e solicita login, sem perda de progresso |
| Queda de rede/processo | Retomada por `deltaLink` + estado do item; lote atômico |
| Delta 410 | Ressincronização completa da pasta, reconciliação por ID |
| Miniatura indisponível | `FALHA_ANALISE` com motivo; não bloqueia lote; retry pela UI |
| Falha parcial ao mover/restaurar | Diário intenção→resultado; reconciliação com estado real no OneDrive |
| Pasta original apagada | Recria caminho; fallback `Restaurados` + aviso |
| Conflito de nome | Rename automático; nome original usado na restauração |
| Modelo novo pior | Holdout; mantém versão anterior; Automático só com versão aprovada |
| Disco temporário | Teto configurável (2 GB); extração pausa ao atingir |
| Exclusão acidental | Somente da quarentena, confirmação com contagem; lixeira do OneDrive |
| Segurança local | Bind `127.0.0.1`, CSRF em mutações, tokens no Keychain, logs sem URLs pré-autenticadas/tokens/nomes de arquivo |

## 6. Testes

| Camada | Estratégia |
|---|---|
| Unitários (pytest) | Features de metadados, máquina de estados, backoff/AIMD, regras de proteção, seleção de versão de modelo |
| Integração Graph | Graph fake (respx): delta, 410, 429 + `Retry-After`, conflito de nome, item apagado no meio do lote, token expirado. Nenhum teste automatizado toca OneDrive real |
| Resiliência | Kill-and-resume em varredura, extração e movimentação; sem duplicação/perda; reconciliação do diário |
| Classificador | Dataset de referência sintético/público (sem fotos pessoais no repo); regressão de precisão; tempo de retreino com 50 mil vetores sintéticos |
| Volumetria | 50 mil itens fake: tempo de varredura, pico de temporário ≤ 2 GB, disco final dentro da meta |
| Telemetria | Testes com `InMemoryMetricReader`/`InMemorySpanExporter` validando nomes, unidades e atributos das métricas e ausência de dados sensíveis |
| Spike | CLIP vs SigLIP em ~300 imagens reais rotuladas |
| E2E manual | Conta pessoal real em pasta de teste |

## 7. Observabilidade (OpenTelemetry)

### 7.1 Princípios

- SDK OpenTelemetry Python para traces, métricas e logs, com `service.name=onedrive-cleaner` e `service.version`.
- Exportação OTLP para endpoint **local** (padrão `http://127.0.0.1:4317`). Stack local opcional via container `grafana/otel-lgtm` (Grafana + Tempo + Loki + Prometheus/Mimir). Telemetria não sai da máquina por padrão.
- Exportador indisponível não bloqueia a aplicação (export assíncrono em batch; falhas apenas logadas localmente).
- Auto-instrumentação: FastAPI (server), httpx (cliente Graph), logging (correlação `trace_id`/`span_id`), system metrics (CPU, memória, disco do processo).
- **Masking:** proibido em atributos/logs: nomes de arquivo, caminhos, URLs pré-autenticadas, tokens, e-mail da conta. Identificação de item somente por ID interno.
- **Cardinalidade:** atributos restritos a enumerações fechadas (operação, resultado, categoria, modo, estado). `model_version` nunca como atributo de métrica; usar `role=current|candidate`. Categoria criada pelo utilizador é limitada pela quantidade de categorias ativas (baixa cardinalidade).

### 7.2 Traces (spans principais)

| Span | Atributos |
|---|---|
| `odc.job.scan` | `odc.scan.mode` (full/delta), `odc.folder.id` (ID, não caminho) |
| `odc.job.extract.batch` | `odc.batch.size`, `odc.batch.result` |
| `odc.job.classify` | `odc.model.role` |
| `odc.model.train` | `odc.labels.count`, `odc.train.outcome` (promoted/rejected) |
| `odc.quarantine.move` / `.restore` / `.delete` | `odc.action.mode`, `odc.action.result` |
| `odc.journal.reconcile` | `odc.reconcile.outcome` |
| `odc.cleanup` | `odc.cleanup.trigger` (job_end/startup) |
| HTTP Graph (auto) | `http.request.method`, `http.response.status_code`, `odc.graph.operation` |

### 7.3 Catálogo de métricas

> Versão canônica (com atributos enumerados e gatilhos de emissão): `docs/specs/onedrive-cleaner/plan.md`, seção 5.3.3. Em caso de divergência, prevalece o plan.

**Integração Graph / autenticação**

| Métrica | Tipo | Unidade | Atributos | Finalidade |
|---|---|---|---|---|
| `odc.graph.requests` | Counter | `{request}` | `operation`, `status_code` | Volume e taxa de erro por operação |
| `odc.graph.request.duration` | Histogram | `s` | `operation`, `status_code` | Latência p50/p95/p99 |
| `odc.graph.throttled` | Counter | `{response}` | `operation`, `status_code` (429/503) | Pressão de throttling |
| `odc.graph.retry_after` | Histogram | `s` | `operation` | Tempo de espera imposto pela Graph |
| `odc.graph.retries` | Counter | `{retry}` | `operation`, `outcome` (success/exhausted) | Eficácia do retry |
| `odc.graph.concurrency.limit` | Gauge | `{request}` | — | Limite atual do controle AIMD |
| `odc.graph.inflight` | UpDownCounter | `{request}` | — | Requests em andamento |
| `odc.auth.token_refresh` | Counter | `{refresh}` | `result` | Renovações de token |
| `odc.auth.login_required` | Counter | `{event}` | `reason` (expired/revoked) | Interrupções que exigem login |

**Varredura**

| Métrica | Tipo | Unidade | Atributos | Finalidade |
|---|---|---|---|---|
| `odc.scan.items` | Counter | `{item}` | `change` (new/updated/deleted), `mode` | Itens descobertos/alterados |
| `odc.scan.duration` | Histogram | `s` | `mode` | Tempo de varredura completa vs delta |
| `odc.scan.pages` | Counter | `{page}` | `mode` | Paginação da delta |
| `odc.scan.delta_resets` | Counter | `{reset}` | — | Ocorrências de 410 (resync) |
| `odc.scan.last_success` | Gauge | `s` (epoch) | — | Frescor da última varredura |

**Pipeline / jobs**

| Métrica | Tipo | Unidade | Atributos | Finalidade |
|---|---|---|---|---|
| `odc.items.by_state` | Observable Gauge | `{item}` | `state` | Distribuição do acervo por estado |
| `odc.pipeline.backlog` | Observable Gauge | `{item}` | `stage` (extract/classify) | Trabalho pendente |
| `odc.jobs` | Counter | `{job}` | `type`, `result` | Execuções de jobs |
| `odc.job.duration` | Histogram | `s` | `type`, `result` | Duração por tipo de job |
| `odc.jobs.resumed` | Counter | `{job}` | `type` | Retomadas após queda |
| `odc.extract.items` | Counter | `{item}` | `result`, `failure_reason` | Sucesso/falha de análise |
| `odc.extract.batch.duration` | Histogram | `s` | `result` | Tempo por lote |
| `odc.extract.thumbnail.size` | Histogram | `By` | — | Volume baixado por miniatura |
| `odc.embedding.duration` | Histogram | `s` | `device` (mps/cpu) | Tempo de inferência por lote |
| `odc.embedding.items` | Counter | `{item}` | `device` | Throughput (rate no dashboard) |

**Classificação / aprendizado**

| Métrica | Tipo | Unidade | Atributos | Finalidade |
|---|---|---|---|---|
| `odc.model.train.duration` | Histogram | `s` | `outcome` | SLA de retreino |
| `odc.model.trainings` | Counter | `{training}` | `outcome` (promoted/rejected), `trigger` (threshold/manual) | Evolução do modelo |
| `odc.model.precision` | Gauge | `1` | `category`, `role` | Precisão por categoria (vigente vs candidato) |
| `odc.model.recall` | Gauge | `1` | `category`, `role` | Recall por categoria |
| `odc.model.removable_precision` | Gauge | `1` | `role` | Métrica do gate Evolutivo (≥ 0,98) |
| `odc.model.labels` | Observable Gauge | `{label}` | `category` | Rótulos do utilizador por categoria |
| `odc.model.auto_mode_eligible` | Gauge | `1` (0/1) | — | Estado do gate Evolutivo |
| `odc.predictions` | Counter | `{prediction}` | `category`, `strategy` (zero_shot/trained) | Volume de predições |
| `odc.prediction.confidence` | Histogram | `1` | `category` | Distribuição de confiança |
| `odc.review.decisions` | Counter | `{decision}` | `outcome` (confirmed/corrected), `predicted_category`, `final_category` | Matriz de confusão operacional |
| `odc.review.queue` | Observable Gauge | `{item}` | — | Itens aguardando revisão |

**Quarentena / ações**

| Métrica | Tipo | Unidade | Atributos | Finalidade |
|---|---|---|---|---|
| `odc.quarantine.moves` | Counter | `{item}` | `mode` (manual/auto), `result` | Itens movidos |
| `odc.quarantine.restores` | Counter | `{item}` | `reason` (false_positive/reclassified/manual), `result`, `fallback` (bool) | Restaurações e falsos positivos |
| `odc.quarantine.deletes` | Counter | `{item}` | `result` | Exclusões confirmadas |
| `odc.quarantine.items` | Observable Gauge | `{item}` | `category` | Tamanho da quarentena |
| `odc.quarantine.bytes` | Observable Gauge | `By` | — | Espaço liberável no OneDrive |
| `odc.journal.pending` | Observable Gauge | `{operation}` | `operation` | Intenções sem resultado (risco de inconsistência) |
| `odc.journal.reconciled` | Counter | `{operation}` | `outcome` | Resultado da reconciliação |

Taxa de falso positivo = `rate(odc.quarantine.restores{reason="false_positive"}) / rate(odc.quarantine.moves)` (derivada no dashboard).

**Recursos locais**

| Métrica | Tipo | Unidade | Atributos | Finalidade |
|---|---|---|---|---|
| `odc.disk.usage` | Observable Gauge | `By` | `component` (temp/db/model/logs) | Footprint em disco |
| `odc.disk.cleanup.freed` | Counter | `By` | `trigger` | Eficácia da limpeza |
| `odc.disk.temp_cap.pauses` | Counter | `{pause}` | — | Pausas por teto de temporário |
| `process.*` / `system.*` (auto) | — | — | — | CPU, memória, threads do processo |
| `http.server.request.duration` (auto) | Histogram | `s` | rota, método, status | Latência da UI |

### 7.4 Logs

Logs estruturados JSON via bridge OTel (correlação `trace_id`/`span_id`), também gravados em arquivo local com rotação. Níveis: `INFO` para ciclo de vida de jobs e ações; `WARN` para throttling, fallback e retomada; `ERROR` para falhas de item e reconciliação. Mesmas regras de masking da seção 7.1.

### 7.5 Dashboards e alertas (stack local)

- Dashboard "OneDrive Cleaner — Operação": acervo por estado, backlog, throughput de varredura/extração, 429/min, latência Graph p95, disco por componente.
- Dashboard "OneDrive Cleaner — Modelo": precisão/recall por categoria (vigente vs candidato), gate Evolutivo, rótulos por categoria, taxa de falso positivo, distribuição de confiança.
- Alertas locais (exibidos também na tela Métricas da UI): `odc.journal.pending > 0` por mais de 5 min; taxa de falso positivo acima de 2% com Automático ativo; `odc.disk.usage{component="temp"}` próximo ao teto; `odc.auth.login_required` incrementado.
