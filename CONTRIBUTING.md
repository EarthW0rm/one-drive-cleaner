# Como contribuir

Obrigado pelo interesse em contribuir. Este projeto segue um fluxo trunk-based simples.

## Fluxo

1. Abra uma issue (bug ou feature) antes de trabalhos grandes, para alinhar o escopo.
2. Crie uma branch curta a partir de `main`: `feat/<assunto>`, `fix/<assunto>` ou `chore/<assunto>`.
3. Faça commits pequenos e coerentes com a mudança.
4. Abra um pull request para `main` usando o template.
5. Todos os status checks precisam passar. A integração é feita por **squash merge**, e a branch é apagada após o merge.

Não há commit direto em `main`.

## Mensagens de commit

Use [Conventional Commits](https://www.conventionalcommits.org/):

- tipo e escopo em inglês;
- descrição em português (PT-BR);
- modo imperativo, sem ponto final.

Exemplo: `feat(scan): adiciona varredura incremental da pasta de câmera`

Tipos: `feat`, `fix`, `perf`, `docs`, `refactor`, `test`, `build`, `ci`, `chore`, `style`, `revert`.

Escopos permitidos: `auth`, `graph`, `scan`, `features`, `ml`, `jobs`, `quarantine`, `storage`, `web`, `ui`, `telemetry`, `ci`, `release`, `deps`, `repo`.

Como o squash merge usa o título do pull request como mensagem final, o título também precisa seguir esse formato.

## Instalação dos hooks

Após clonar o repositório, instale os hooks de `pre-commit` (commit e mensagem de commit):

```bash
uv sync
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg
```

Os hooks bloqueiam segredos (`gitleaks`), bancos locais (`*.db`, `*.sqlite*`), imagens fora de `frontend/src/assets/` e `tests/fixtures/images/`, arquivos maiores que 1 MB, erros de lint e mensagens de commit fora do padrão. O hook `gitleaks` é compilado pelo `pre-commit` na primeira execução e requer Go instalado.

Não use `--no-verify`. Se um hook bloquear o commit, corrija a causa. Tokens de teste devem usar o prefixo `FAKE-`, que está na allowlist de `.gitleaks.toml`.

## Testes e qualidade

- Todo código de produção novo tem teste unitário correspondente.
- Cobertura mínima nos arquivos alterados: 90% de linhas e 85% de branches.
- Todo arquivo-fonte novo tem o cabeçalho `SPDX-License-Identifier: GPL-3.0-only`.
- Testes não acessam o OneDrive real nem usam fotos pessoais. Use fixtures sintéticas.
- Tokens em fixtures devem ter o prefixo `FAKE-`.

Comandos principais (disponíveis após a configuração do projeto):

```bash
make lint   # análise estática (ruff, mypy strict, import-linter, deptry, vulture; ESLint, Prettier, tsc, knip)
make test   # testes unitários e de integração
```

## Dados pessoais e segredos

Nunca inclua em commits, issues ou pull requests: fotos ou miniaturas reais, `client_id` de aplicativo, tokens, arquivos `.env`, bancos SQLite com dados de uso ou caminhos de pastas pessoais. O `.gitignore` cobre os padrões mais comuns, mas a revisão do conteúdo do commit continua sendo responsabilidade de quem contribui.

## Código de conduta

Ao participar, você concorda com o [Código de Conduta](CODE_OF_CONDUCT.md).

## Licença

Contribuições são licenciadas sob a GPL-3.0-only, a mesma licença do projeto.
