# Benchmark de modelos de visão

Métricas agregadas; nenhuma imagem, nome, caminho ou identificador da amostra.

## Configuração

- Dispositivo: mps (pedido: auto)
- Validação cruzada: 4 folds estratificados, seed 42
- Limiar de confiança de `removable_precision`: 0,98
- Tamanho de lote: 16
- Tamanhos de miniatura simulados (px): 384, 800

## Amostra

| Categoria | Imagens |
|---|---|
| screenshot | 186 |
| real_photo | 202 |
| meme_sticker | 4 |
| document_receipt | 0 |

Corrompidas ignoradas: 0. Em `_revisar/` ignoradas: 0.

Tamanho de miniatura customizado suportado pela Graph (`custom_size_supported`): sim.
Miniaturas de `_thumbs/` usadas por tamanho: 384 px: 392/392, 800 px: 392/392.
Linhas `+ meta`: embedding + features de metadados do manifesto (informativas; a recomendação considera só as linhas sem metadados).

## Resultados

| Modelo | Miniatura (px) | removable_precision@0,98 | Suporte | Acurácia | Macro-F1 | s/img (MPS) | Projeção 5 mil (min) | Projeção 50 mil (min) | Pico de memória (MB) | Pesos (MB) | Vetores 50 mil (MB) | Licença dos pesos | Elegível |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ViT-B-32/laion2b_s34b_b79k | 384 | 100,0% | 20 | 87,0% | 58,3% | 0,0042 | 0,4 | 3,5 | 612 | 605 | 51,2 | MIT | sim |
| ViT-B-32/laion2b_s34b_b79k + meta | 384 | 98,9% | 89 | 97,4% | 82,0% | 0,0042 | 0,4 | 3,5 | 612 | 605 | 51,2 | MIT | sim |
| ViT-B-32/laion2b_s34b_b79k | 800 | 100,0% | 5 | 84,4% | 69,8% | 0,0051 | 0,4 | 4,3 | 612 | 605 | 51,2 | MIT | sim |
| ViT-B-32/laion2b_s34b_b79k + meta | 800 | 100,0% | 62 | 97,4% | 78,8% | 0,0051 | 0,4 | 4,3 | 612 | 605 | 51,2 | MIT | sim |
| ViT-SO400M-14-SigLIP-384/webli | 384 | 100,0% | 21 | 86,5% | 57,9% | 0,1628 | 13,6 | 135,6 | 3533 | 3512 | 115,2 | Apache-2.0 | sim |
| ViT-SO400M-14-SigLIP-384/webli + meta | 384 | 100,0% | 59 | 97,2% | 78,6% | 0,1628 | 13,6 | 135,6 | 3533 | 3512 | 115,2 | Apache-2.0 | sim |
| ViT-SO400M-14-SigLIP-384/webli | 800 | 100,0% | 19 | 83,7% | 56,0% | 0,1653 | 13,8 | 137,8 | 3533 | 3512 | 115,2 | Apache-2.0 | sim |
| ViT-SO400M-14-SigLIP-384/webli + meta | 800 | 100,0% | 60 | 97,2% | 78,6% | 0,1653 | 13,8 | 137,8 | 3533 | 3512 | 115,2 | Apache-2.0 | sim |

## Recomendação

**Modelo:** `ViT-B-32/laion2b_s34b_b79k` com miniatura de 384 px.

Critério: maior removable_precision.
Empate técnico (<= 1 p.p.) entre 4 configurações: escolhida a de menor tempo projetado (desempate: menor disco de pesos + vetores).

## Avisos

- Folds reduzidos de 5 para 4 (categoria com poucas imagens).
- Amostra desbalanceada: categorias com menos de 30 imagens ou ausentes (document_receipt, meme_sticker); a métrica é pouco confiável.
- A linha recomendada não comprova removable_precision >= 0,98 com suporte >= 30: decisão explícita do utilizador necessária (rever amostra/categorias ou aceitar menor precisão com modo Automático desabilitado).

## Notas de método

- Tempo por imagem = pré-processamento + embedding em lotes após aquecimento; não inclui o download das miniaturas (plan 8.1).
- Pico de memória: memória alocada na MPS; em CPU, pico de RSS do processo (acumulado entre modelos da mesma execução).
- Vetores 50 mil: float16 (`dim x 2` bytes por item).
- `removable_precision`: entre as predições removíveis com confiança >= 0,98, proporção cujo rótulo também é removível (categorias removíveis: screenshot, meme_sticker, document_receipt).
