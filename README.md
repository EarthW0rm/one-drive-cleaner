# OneDrive Cleaner

Identifica imagens que não são fotos (prints de tela, memes, documentos fotografados) no OneDrive pessoal e as move para uma **quarentena reversível**. Nada é excluído sem confirmação explícita do utilizador, e cada ação pode ser desfeita.

> **Aviso de não afiliação:** este projeto é independente e não é afiliado, endossado nem mantido pela Microsoft Corporation. "OneDrive", "Microsoft", "Microsoft Entra" e "Microsoft Graph" são marcas de seus respectivos titulares.

## Privacidade

- A análise das imagens é **100% local**: nenhuma foto, miniatura, metadado ou resultado de classificação é enviado a serviços de terceiros.
- Os tokens de acesso são guardados apenas no Keychain do macOS.
- O projeto não possui segredos embutidos (cliente público, sem `client secret`). Nenhum dado pessoal é versionado neste repositório.
- A telemetria local não registra nomes de arquivos, caminhos, URLs pré-autenticadas, tokens nem endereços de e-mail.

## Status

Projeto em desenvolvimento inicial (MVP). Ainda não há release publicada.

## Pré-requisitos

- macOS
- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Node.js 22 e [pnpm](https://pnpm.io/) (somente para compilar a interface a partir do código-fonte)
- Conta pessoal Microsoft com OneDrive

## Registro do aplicativo no Microsoft Entra

O cliente OAuth não é distribuído com o código. Quem instala ou compila o projeto registra o próprio aplicativo:

1. Acesse o [Centro de Administração do Microsoft Entra](https://entra.microsoft.com) e registre um novo aplicativo.
2. Em **Tipos de conta com suporte**, selecione **Contas pessoais da Microsoft**.
3. Em **Autenticação**, adicione a plataforma **Mobile and desktop applications** com o redirect URI `http://localhost`.
4. Copie o **Application (client) ID** gerado.
5. Configure o identificador localmente, por variável de ambiente ou pelo arquivo de configuração do utilizador:

   ```bash
   export ODC_CLIENT_ID="<application-client-id>"
   ```

   Alternativamente, o valor pode ser definido em `~/Library/Application Support/OneDriveCleaner/config.toml`. Este arquivo não é versionado.

Não cole o `client_id` em issues, pull requests ou commits.

## Instalação

Instale a partir do wheel publicado na página de [Releases](https://github.com/EarthW0rm/one-drive-cleaner/releases):

```bash
uv tool install "https://github.com/EarthW0rm/one-drive-cleaner/releases/download/v<versão>/<arquivo>.whl"
```

Substitua `<versão>` e `<arquivo>` pelos valores da release escolhida.

## Desenvolvimento

Os comandos de build, teste e lint estão no `Makefile` (a ser criado nas próximas tarefas de implementação). Consulte [CONTRIBUTING.md](CONTRIBUTING.md) para o fluxo de contribuição.

## Monitoramento local

Opcional. Uma stack local (`grafana/otel-lgtm`: collector OTLP, Tempo, Loki, Prometheus e Grafana) recebe traces, métricas e logs da aplicação e traz os dashboards "Operação" e "Modelo" e 6 alertas provisionados. Requer Docker. As portas ficam publicadas somente em `127.0.0.1`, e nenhum dado sai da máquina.

```bash
make obs-up      # sobe a stack (Grafana em http://127.0.0.1:3000, OTLP em 127.0.0.1:4317)
odc              # a aplicação exporta para 127.0.0.1:4317 por padrão
make obs-down    # derruba a stack (os dados ficam no volume odc-lgtm-data)
```

Sem a stack, a aplicação funciona normalmente e a tela Métricas continua disponível. Para apagar também os dados: `docker compose -f deploy/observability/docker-compose.yml down -v`. O alerta "Temporário no limite" assume o teto padrão de 2 GiB; ajuste o limiar em `deploy/observability/grafana/provisioning/alerting/odc-alerts.yaml` se alterar o teto nas configurações.

## Segurança

Para reportar vulnerabilidades, siga o procedimento em [SECURITY.md](SECURITY.md). Não abra issues públicas para esse tipo de problema.

## Licença

Distribuído sob a [GNU General Public License v3.0 only](LICENSE) (`GPL-3.0-only`).
