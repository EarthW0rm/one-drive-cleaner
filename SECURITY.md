# Política de segurança

## Versões suportadas

Somente a versão mais recente publicada em [Releases](https://github.com/EarthW0rm/one-drive-cleaner/releases) recebe correções de segurança. Enquanto o projeto estiver em `0.x`, não há backport para versões anteriores.

| Versão | Suportada |
|--------|-----------|
| Última release | Sim |
| Versões anteriores | Não |

## Como reportar uma vulnerabilidade

Não abra issues públicas para vulnerabilidades.

Use o **private vulnerability reporting** do GitHub: na aba **Security** do repositório, selecione **Report a vulnerability**. O relatório fica visível apenas para o mantenedor.

Inclua, quando possível:

- descrição do problema e impacto esperado;
- passos para reproduzir, com dados fictícios (nunca fotos ou tokens reais);
- versão afetada e ambiente (versão do macOS, Python).

## Prazos

O mantenedor busca confirmar o recebimento em até 7 dias e apresentar uma avaliação inicial em até 14 dias. Os prazos são estimativas de melhor esforço para um projeto pessoal.

## Escopo

Estão no escopo: falhas que permitam acesso indevido a arquivos do utilizador, exposição de tokens, exclusão ou movimentação de arquivos sem confirmação, e vazamento de dados pessoais por telemetria ou logs.

## Releases e rotação da chave do GitHub App

As releases são criadas pelo workflow `release-please.yml`, que usa um GitHub App com permissões mínimas (`contents: write` e `pull-requests: write`). A chave privada fica apenas no secret `RELEASE_APP_PRIVATE_KEY` do environment `release` (restrito à `main`); o ID do App fica na variável `RELEASE_APP_ID`. Os artefatos publicados têm atestado de proveniência (`gh attestation verify <arquivo> --repo EarthW0rm/one-drive-cleaner`) e `SHA256SUMS`.

Rotação (periódica, ou imediata se a chave vazar):

1. Nas configurações do GitHub App, gere uma nova chave privada.
2. Atualize o secret `RELEASE_APP_PRIVATE_KEY` do environment `release` com o conteúdo do novo `.pem`.
3. Revogue a chave antiga nas configurações do App.
4. Apague o `.pem` local e confirme que o workflow `release-please` seguinte conclui com sucesso.

Em caso de vazamento, revise também as releases e tags criadas desde a exposição.
