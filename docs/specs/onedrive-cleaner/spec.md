# Especificação Funcional: OneDrive Cleaner — Identificação e Quarentena de Imagens Não-Foto

- **Status:** Aprovado (2026-11-17)
- **Data:** 2026-11-17
- **Insumo:** [`docs/plans/2026-11-17-onedrive-cleaner-design.md`](../../plans/2026-11-17-onedrive-cleaner-design.md)

## 1. Visão Geral e Objetivo

- **O Problema:** A pasta de fotos do OneDrive pessoal (upload automático do celular) acumula milhares de imagens que não são fotos: prints de tela (conversas de WhatsApp, telas de apps), memes, figurinhas, documentos e recibos fotografados. Esses itens normalmente são temporários, ocupam espaço e poluem o acervo de fotos reais (família, animais, viagens). A triagem manual é inviável na escala do acervo. Não é possível descartar por origem: imagens recebidas pelo WhatsApp também incluem fotos de família enviadas por terceiros.
- **A Solução (O Que):** Aplicação executada na máquina do utilizador que se conecta à conta pessoal do OneDrive, analisa as imagens das pastas escolhidas, sugere uma categoria para cada imagem (print de tela, foto real, meme/figurinha, documento/recibo e categorias criadas pelo utilizador), aprende com as confirmações e correções do utilizador, e move os itens não-foto para uma quarentena reversível no próprio OneDrive. Da quarentena, o utilizador restaura falsos positivos à pasta original (ensinando o sistema) ou envia os itens para a lixeira do OneDrive.
- **O Valor (Porque):**
  - Liberar espaço no OneDrive e devolver ao acervo de fotos apenas fotos reais.
  - Reduzir o esforço de triagem: o utilizador revisa primeiro os casos duvidosos e o sistema melhora a cada correção, até poder operar de forma automática com precisão medida.
  - Garantir privacidade: nenhuma imagem é analisada fora da máquina do utilizador.
  - Garantir segurança: nenhuma foto real é perdida — toda movimentação é reversível e toda exclusão é explícita e recuperável pela lixeira do OneDrive.

### 1.1 Escopo

| Dentro do escopo (MVP) | Fora do escopo |
|---|---|
| Conta Microsoft pessoal (OneDrive pessoal) | Contas corporativas / OneDrive for Business |
| Arquivos de imagem | Vídeos, documentos não-imagem |
| Pastas escolhidas pelo utilizador (padrão: Camera Roll), com subpastas | Varredura obrigatória do OneDrive inteiro |
| Análise 100% local | Análise por serviços de terceiros |
| Classificação multiclasse extensível com aprendizado a partir do utilizador | Detecção de duplicatas, fotos borradas ou similares |
| Quarentena, restauração e envio à lixeira do OneDrive | Exclusão permanente (fora da lixeira) |
| Acompanhamento por indicadores de monitoramento locais | Uso multiutilizador / hospedagem em servidor |
| Base para evolução futura do aprendizado (reaproveitamento dos rótulos) | A evolução em si (ajuste profundo do modelo) |

## 2. Jornadas do Usuário (User Journeys)

**Ator principal:** Utilizador — dono da conta pessoal do OneDrive, executa a aplicação na própria máquina.
**Atores externos:** OneDrive (armazenamento e operações de arquivo); Serviço de identidade da Microsoft (login e autorização).

**J1 — Primeira configuração e login**
1. O utilizador inicia a aplicação; a interface abre no navegador da própria máquina.
2. O utilizador faz login com a conta Microsoft pessoal e autoriza acesso aos arquivos.
3. O sistema lista as pastas do OneDrive e pré-seleciona a pasta Camera Roll.
4. O utilizador confirma ou altera as pastas a varrer e escolhe o modo de automação (padrão: Evolutivo).
5. O sistema cria no OneDrive a estrutura de pastas da aplicação (Quarentena e Restaurados) se ainda não existir.

**J2 — Varredura e análise inicial**
1. O utilizador inicia a varredura.
2. O sistema inventaria as imagens das pastas escolhidas e registra seus metadados (dimensões, dados de câmera, data, nome, origem).
3. O sistema analisa cada imagem localmente, registra o resultado da análise e descarta a cópia temporária da imagem imediatamente após a análise.
4. O sistema sugere uma categoria e um grau de confiança para cada imagem. Antes de existirem classificações do utilizador, a sugestão usa apenas o conhecimento prévio do sistema.
5. A interface exibe o progresso (itens inventariados, analisados, com falha, pendentes) e permite pausar e retomar.
6. Resultado: acervo pré-categorizado e pronto para revisão.

**J3 — Revisão e aprendizado**
1. O utilizador abre a tela de Revisão; o sistema exibe uma grade de miniaturas com a categoria sugerida e a confiança, priorizando os itens em que o sistema tem menos certeza.
2. O utilizador confirma ou corrige a categoria de cada item (individualmente ou em seleção múltipla).
3. Ao atingir a quantidade configurada de novas classificações (ou por comando do utilizador), o sistema reaprende, avalia a nova versão e, se ela não for pior que a vigente, reclassifica todo o acervo.
4. O sistema exibe a precisão atual por categoria e a distância até a meta que libera o modo Automático.

**J4 — Mover para quarentena**
1. Modo Manual: o utilizador seleciona os itens (ou um lote por categoria removível) e confirma; o sistema move os itens para a Quarentena.
2. Modo Automático: o sistema move, ao fim de cada análise ou reaprendizado, os itens de categorias removíveis cuja confiança atinge o limiar configurado.
3. Modo Evolutivo: comporta-se como Manual até a meta de precisão ser atingida; então passa a se comportar como Automático.
4. O sistema registra, para cada item movido, a pasta e o nome originais.

**J5 — Revisão da quarentena: restaurar ou excluir**
1. O utilizador abre a tela de Quarentena e vê os itens agrupados por categoria.
2. **Restaurar (falso positivo):** o utilizador marca itens como indevidos e informa a categoria correta; o sistema registra a correção como aprendizado e devolve os itens à pasta e ao nome originais.
3. Após o reaprendizado, itens da quarentena reclassificados para categorias não removíveis são restaurados automaticamente à origem (RN-17) e listados como restaurados na interface.
4. **Excluir:** o utilizador seleciona itens e solicita exclusão; o sistema exibe a quantidade e o volume a excluir e exige confirmação explícita; os itens vão para a lixeira do OneDrive.

**J6 — Gestão de categorias**
1. O utilizador cria uma categoria nova, informando nome, descrição e se ela é removível.
2. O utilizador classifica exemplos na nova categoria; o sistema passa a sugeri-la após o reaprendizado.
3. O utilizador pode renomear ou arquivar categorias (exceto "Foto real").

**J7 — Varreduras subsequentes**
1. O utilizador (ou a próxima execução) inicia nova varredura.
2. O sistema processa apenas imagens novas, alteradas ou removidas desde a varredura anterior.
3. Itens movidos, apagados ou restaurados fora da aplicação têm seu estado atualizado.

**J8 — Acompanhamento**
1. O utilizador consulta a tela de Métricas: acervo por estado, progresso, precisão por categoria, taxa de falso positivo, espaço liberável, uso de disco local e alertas.
2. Os mesmos indicadores ficam disponíveis para a ferramenta de monitoramento local (seção 5.4).

**Jornada alternativa — Interrupção**
1. A conexão, a autorização ou a aplicação é interrompida durante uma varredura, análise ou movimentação.
2. Ao reiniciar (e refazer login, se necessário), o sistema retoma do ponto em que parou, sem duplicar nem perder itens, e reconcilia operações de movimentação que ficaram incompletas.

## 3. Regras de Negócio e Restrições

| # | Regra | Tipo |
|---|---|---|
| RN-01 | Somente contas Microsoft pessoais são suportadas. Uma conta ativa por vez por instalação; trocar de conta exige logout, e os registros de cada conta são isolados. | Restritiva |
| RN-02 | Imagens e quaisquer derivados delas são analisados exclusivamente na máquina do utilizador. Nenhuma imagem, miniatura ou derivado é enviado a terceiros (além das operações no próprio OneDrive). | Restritiva |
| RN-03 | Cópias temporárias de imagens são removidas imediatamente após a análise de cada item. O espaço temporário em uso não ultrapassa 2 GB (configurável); ao atingir o teto, a análise pausa até haver espaço. Ao fim de cada execução, o espaço total ocupado pela aplicação na máquina não ultrapassa 500 MB, desconsiderando os arquivos do modelo de visão (que podem permanecer instalados). | Obrigatória |
| RN-04 | A aplicação guarda localmente, de forma persistente, os metadados, resultados de análise, sugestões, classificações do utilizador, versões do aprendizado e o diário de operações — esses registros são a fonte para pré-categorização, parâmetros e resultados. | Obrigatória |
| RN-05 | A varredura abrange as pastas escolhidas pelo utilizador e suas subpastas; padrão: Camera Roll. As pastas da própria aplicação (Quarentena, Restaurados) nunca são varridas como acervo. | Obrigatória |
| RN-06 | Somente arquivos de imagem são considerados; demais arquivos são ignorados. | Restritiva |
| RN-07 | Após a primeira varredura completa de uma pasta, as seguintes processam apenas as mudanças. | Obrigatória |
| RN-08 | Categorias iniciais: **Print de tela**, **Foto real**, **Meme/figurinha**, **Documento/recibo**. O utilizador cria, renomeia e arquiva categorias; cada categoria tem o atributo "removível" (pode ir para quarentena). | Obrigatória |
| RN-09 | "Foto real" é categoria protegida: nunca é removível, não pode ser arquivada nem renomeada, e itens nela classificados nunca vão para a quarentena. | Restritiva |
| RN-10 | Origem e nome do arquivo (ex.: padrão de nome do WhatsApp ou de print) são sinais auxiliares, nunca critério único de classificação. | Restritiva |
| RN-11 | Toda confirmação ou correção do utilizador vira exemplo de aprendizado, incluindo a restauração de um item da quarentena (correção de falso positivo). A classificação dada pelo utilizador prevalece sobre qualquer sugestão do sistema. | Obrigatória |
| RN-12 | O reaprendizado ocorre a cada 20 novas classificações do utilizador (configurável) ou sob comando. | Condicional |
| RN-13 | Uma nova versão do aprendizado só substitui a vigente se sua precisão nas categorias removíveis, medida em exemplos reservados para avaliação, não for inferior à da vigente. Caso contrário, a vigente é mantida e a comparação é exibida. | Restritiva |
| RN-14 | Modos de automação: **Manual** (nada é movido sem confirmação), **Automático** (move por limiar de confiança) e **Evolutivo** (padrão). | Obrigatória |
| RN-15 | O modo Evolutivo libera o comportamento Automático somente quando a precisão nas categorias removíveis for ≥ 98% e existirem ≥ 200 classificações do utilizador. Se a precisão cair abaixo de 98%, volta ao comportamento Manual. | Condicional |
| RN-16 | O comportamento Automático move apenas itens de categorias removíveis com confiança ≥ 0,98 (limiar configurável). | Condicional |
| RN-17 | Itens em quarentena reclassificados, após reaprendizado, para categoria não removível são restaurados automaticamente à origem, em qualquer modo de automação (direção conservadora), com registro no diário e aviso na interface. | Obrigatória |
| RN-18 | A quarentena é uma pasta dedicada no OneDrive do utilizador (`/OneDrive Cleaner/Quarentena`). Para cada item movido são registrados pasta e nome originais. | Obrigatória |
| RN-19 | A restauração devolve o item à pasta e ao nome originais; se a pasta não existir, ela é recriada; se não for possível, o item vai para `/OneDrive Cleaner/Restaurados` e o utilizador é avisado. | Obrigatória |
| RN-20 | Exclusão somente de itens que estão na quarentena, mediante confirmação explícita que exibe quantidade e volume. O destino é sempre a lixeira do OneDrive; a aplicação nunca exclui permanentemente. Nenhuma exclusão ocorre de forma automática, em nenhum modo. | Restritiva |
| RN-21 | A aplicação nunca altera o conteúdo de arquivos; altera apenas localização, nome (quando necessário para resolver conflito) e envia à lixeira (RN-20). | Restritiva |
| RN-22 | Toda movimentação, restauração e exclusão é registrada em diário (item, origem, destino, momento, resultado) antes e depois da operação. | Obrigatória |
| RN-23 | A interface é acessível somente a partir da própria máquina do utilizador. | Restritiva |
| RN-24 | O comportamento da aplicação é monitorável pelos indicadores da seção 5.4, exportados em padrão aberto de telemetria para ferramenta de monitoramento **local**. Indicadores, registros e rastros nunca contêm nomes de arquivo, caminhos, links de acesso, credenciais ou o e-mail da conta. | Obrigatória |
| RN-25 | A indisponibilidade da ferramenta de monitoramento nunca interrompe nem degrada o funcionamento da aplicação. | Restritiva |
| RN-26 | As classificações do utilizador são preservadas mesmo após o item ser excluído, para reaproveitamento em evoluções futuras do aprendizado. | Obrigatória |
| RN-27 | Em cada rodada do comportamento Automático, no máximo 30% do acervo ativo pode ser movido para a quarentena; o excedente permanece aguardando revisão do utilizador e um alerta é registrado. | Restritiva |

## 4. Casos de Borda e Fluxos de Exceção (Zero Trust)

| Cenário | Comportamento Esperado | Severidade |
|---|---|---|
| OneDrive limita a taxa de requisições | Respeitar o tempo de espera informado, reduzir o ritmo automaticamente e retomar gradualmente; nunca falhar o lote por limitação | Alta |
| OneDrive indisponível / rede cai | Pausar o trabalho em andamento, tentar novamente com espera crescente (máx. 5 tentativas por operação); persistindo, pausar o job e exibir o motivo; retomar do ponto de parada | Alta |
| Autorização expira ou é revogada | Renovar silenciosamente; se impossível, pausar jobs e solicitar novo login, sem perda de progresso | Alta |
| Aplicação encerrada no meio de um lote | Ao reiniciar, retomar do último ponto confirmado; no pior caso refazer apenas o lote em andamento; nenhum item duplicado ou perdido | Alta |
| Histórico de mudanças do OneDrive expirado | Refazer o inventário completo da pasta, preservando classificações e histórico dos itens já conhecidos | Média |
| Metadados ausentes, inconsistentes ou malformados | Tratar como ausentes; não bloquear a análise; nunca usar metadado inválido como evidência | Média |
| Imagem sem miniatura, corrompida ou formato não suportado | Marcar item como falha de análise com o motivo; não bloquear o lote; permitir nova tentativa pela interface | Média |
| Item apagado ou alterado entre o inventário e a análise | Atualizar o estado do item (removido externamente / re-análise); não falhar o lote | Média |
| Movimentação/restauração falha no meio de um lote | Cada item é tratado isoladamente; diário registra intenção e resultado; na retomada, consultar o estado real no OneDrive e reconciliar | Crítica |
| Conflito de nome no destino | Renomear automaticamente no destino, registrar o novo nome e restaurar com o nome original | Média |
| Pasta original não existe mais na restauração | Recriar a pasta; se impossível, restaurar em `Restaurados` e avisar | Alta |
| Item movido, apagado ou restaurado fora da aplicação | Detectar na próxima varredura e atualizar o estado, sem tentar desfazer a ação do utilizador | Média |
| Pasta de quarentena apagada ou renomeada pelo utilizador | Detectar, recriar a estrutura e reconciliar os itens pelo identificador do OneDrive | Alta |
| Nova versão do aprendizado com precisão inferior | Manter a versão vigente, registrar a comparação e exibir ao utilizador | Alta |
| Categoria com poucos exemplos | Continuar sugerindo pelo conhecimento prévio; sinalizar baixa confiança na interface | Baixa |
| Utilizador classifica o mesmo item de formas diferentes | Prevalece a classificação mais recente; histórico preservado | Baixa |
| Tentativa de mover item "Foto real" para a quarentena | Bloquear a operação e informar o utilizador | Crítica |
| Tentativa de excluir item fora da quarentena | Bloquear a operação | Crítica |
| Espaço temporário atinge o teto | Pausar a análise até a limpeza liberar espaço; registrar o evento | Média |
| Disco local sem espaço para os registros persistentes | Interromper com mensagem clara, sem corromper registros já gravados | Alta |
| Ferramenta de monitoramento local indisponível | Seguir operando normalmente; descartar ou reenviar telemetria sem bloquear (RN-25) | Baixa |
| Requisição à interface de origem externa à máquina | Rejeitar | Crítica |
| Ação que altera dados sem token de sessão válido da interface | Rejeitar | Crítica |

## 5. Critérios de Sucesso e SLAs

### 5.1 Critérios Funcionais

- [ ] Jornadas J1 a J8 e a jornada de interrupção executam com sucesso em conta pessoal real (pasta de teste).
- [ ] Regras RN-01 a RN-27 validadas por testes automatizados ou checklist manual.
- [ ] Nenhuma foto classificada como "Foto real" pelo utilizador vai para a quarentena.
- [ ] Toda restauração devolve o item à pasta e ao nome originais (ou ao fallback de RN-19).
- [ ] Interrupção em qualquer etapa resulta em retomada sem duplicação nem perda.
- [ ] Ao fim de cada execução, nenhuma cópia temporária de imagem permanece na máquina.

### 5.2 SLAs de Desempenho (máquina de referência: Mac com chip M3, 16 GB)

| Indicador | Meta |
|---|---|
| Volumetria suportada | ~5 mil imagens no início; comportamento estável com ≥ 50 mil |
| Análise inicial completa de 5 mil imagens | ≤ 30 min (meta a validar no spike de benchmark) |
| Varredura subsequente sem mudanças | ≤ 1 min |
| Reaprendizado com 50 mil itens analisados | ≤ 30 s |
| Reclassificação do acervo após reaprendizado (50 mil) | ≤ 10 s |
| Carregamento da grade de revisão (100 itens) | ≤ 2 s |
| Perda máxima em interrupção | 1 lote em andamento (refeito na retomada) |
| Espaço temporário em execução | ≤ 2 GB (configurável) |
| Espaço total da aplicação ao final da execução | ≤ 500 MB (sem contar os arquivos do modelo de visão) |

### 5.3 Critérios de Qualidade do Aprendizado

- [ ] Precisão ≥ 98% nas categorias removíveis, com ≥ 200 classificações do utilizador, como condição para o comportamento Automático (RN-15).
- [ ] Taxa de falso positivo na quarentena (restaurações por correção ÷ itens movidos) ≤ 2% com o comportamento Automático ativo.
- [ ] Precisão e recall por categoria visíveis na interface para a versão vigente e a candidata.

### 5.4 Indicadores de Monitoramento Obrigatórios

Catálogo técnico completo (nomes, tipos, unidades, atributos) definido no `plan.md`, a partir da seção 7 do design.

| Grupo | Indicadores |
|---|---|
| Integração com o OneDrive | Volume de requisições e erros por operação; latência (p50/p95/p99); ocorrências de limitação de taxa e tempo de espera imposto; novas tentativas e esgotamentos; ritmo/concorrência atual; renovações de autorização e logins exigidos |
| Varredura | Itens novos/alterados/removidos; duração (completa vs incremental); reinícios de inventário; momento da última varredura bem-sucedida |
| Processamento | Itens por estado; trabalho pendente por etapa; execuções, duração e retomadas de jobs; sucesso/falha de análise por motivo; tempo e vazão da análise; volume baixado |
| Aprendizado | Duração e resultado do reaprendizado (promovido/rejeitado); precisão e recall por categoria (vigente vs candidata); precisão das categorias removíveis; classificações do utilizador por categoria; elegibilidade ao Automático; distribuição de confiança; confirmações vs correções (matriz de confusão operacional); fila de revisão |
| Quarentena | Itens movidos (manual/automático); restaurações por motivo; exclusões; itens e volume em quarentena; taxa de falso positivo; operações pendentes no diário e resultado da reconciliação |
| Recursos locais | Uso de disco por componente (temporário, registros, aprendizado, logs); espaço liberado pela limpeza; pausas por teto de temporário; CPU e memória; latência da interface |
| Alertas | Operação pendente no diário por mais de 5 min; falso positivo > 2% com Automático ativo; temporário próximo ao teto; login exigido; rodada automática limitada pela RN-27 |

### 5.5 Critérios de Qualidade de Engenharia

- [ ] **Todo código possui teste unitário**: cada módulo de produção tem teste correspondente; cobertura mínima de 90% de linhas e 85% de ramificações, tanto no núcleo da aplicação quanto na interface; exceções apenas listadas explicitamente.
- [ ] **Interface** com testes unitários de componentes e telas e testes ponta a ponta automatizados cobrindo as jornadas J1 a J8 e a jornada de interrupção, incluindo verificação de acessibilidade.
- [ ] **Aprendizado** com testes próprios: regras de treino, avaliação e promoção; consistência da análise de imagens; qualidade mínima em conjunto de referência; robustez a variações (redimensionamento, recompressão, recorte, troca de nome do arquivo — RN-10); desempenho (SLAs de 5.2); e regressão de métricas entre versões.
- [ ] Testes de integração (com OneDrive simulado) para todos os casos de borda da seção 4.
- [ ] Teste de volumetria com 50 mil itens simulados cumprindo os SLAs de 5.2.
- [ ] Testes que comprovem a ausência de dados sensíveis em indicadores, rastros e registros (RN-24).
- [ ] Nenhum teste automatizado acessa o OneDrive real ou usa fotos pessoais.
- [ ] Análise estática automática do código (padrões de estilo, erros, tipagem, complexidade, duplicação, segurança, arquitetura em camadas, dependências e código morto) sem violações bloqueantes, como condição para integrar qualquer mudança.
- [ ] Código-fonte público: nenhum segredo, credencial, dado pessoal, banco local ou foto do utilizador é versionado; verificação automática bloqueia a entrada desses itens.
- [ ] Mudanças entram na linha principal somente por revisão com todas as verificações automáticas aprovadas; histórico protegido contra reescrita e exclusão.
- [ ] Toda versão publicada segue versionamento semântico derivado automaticamente das mensagens de mudança padronizadas, com notas de versão e pacote instalável verificável (checksums e procedência).

## 6. Glossário

| Termo | Definição neste contexto |
|---|---|
| Acervo | Conjunto de imagens das pastas escolhidas pelo utilizador para varredura |
| Foto real | Imagem capturada por câmera retratando cenas do mundo real (pessoas, animais, lugares, objetos); categoria protegida |
| Print de tela | Captura da tela de um dispositivo (celular ou computador), incluindo conversas de WhatsApp e telas de aplicativos |
| Imagem não-foto | Qualquer imagem classificada em categoria removível (print, meme, figurinha, documento/recibo, categorias do utilizador marcadas como removíveis) |
| Categoria removível | Categoria cujos itens podem ser movidos para a quarentena |
| Varredura | Inventário das imagens do acervo e registro de seus metadados |
| Análise | Processamento local de uma imagem que gera as informações usadas na classificação; a imagem temporária é descartada ao final |
| Sugestão | Categoria proposta pelo sistema para um item, acompanhada de um grau de confiança |
| Confiança | Valor entre 0 e 1 que expressa a certeza do sistema na sugestão |
| Classificação do utilizador (rótulo) | Confirmação ou correção de categoria feita pelo utilizador; fonte do aprendizado |
| Reaprendizado | Geração de nova versão do aprendizado a partir das classificações do utilizador |
| Versão vigente / candidata | Versão do aprendizado em uso / recém-gerada em avaliação |
| Precisão (categorias removíveis) | Proporção de itens sugeridos como removíveis que de fato são removíveis, medida em exemplos reservados para avaliação |
| Falso positivo | Item que não deveria ir para a quarentena (ex.: foto real) e foi movido para lá |
| Quarentena | Pasta dedicada no OneDrive para itens candidatos à exclusão; reversível |
| Restaurar | Devolver um item da quarentena à pasta e ao nome originais |
| Excluir | Enviar item da quarentena para a lixeira do OneDrive (recuperável pelo período de retenção do OneDrive) |
| Diário | Registro de intenção e resultado de cada movimentação, restauração e exclusão |
| Modo Manual / Automático / Evolutivo | Políticas de movimentação para a quarentena (RN-14 a RN-16) |
| Teto de temporário | Limite de espaço em disco para cópias temporárias de imagens durante a análise |
