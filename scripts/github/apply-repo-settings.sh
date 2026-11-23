#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Aplica rulesets e configurações do repositório (plan 11.1 e 11.2). Idempotente: cria ou atualiza
# cada ruleset por nome. Uso: RELEASE_APP_ID=<id> apply-repo-settings.sh [--dry-run] [--repo OWNER/REPO]
#   --dry-run  imprime os payloads e as chamadas; só faz leituras (nenhuma escrita).
#   RELEASE_APP_ID  ID numérico do GitHub App de release (único bypass do ruleset de tags v*);
#                   obrigatório fora do --dry-run.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-EarthW0rm/one-drive-cleaner}"
DRY_RUN=0

usage() {
  sed -n '3,7p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --repo) REPO="${2:?--repo exige OWNER/REPO}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Argumento desconhecido: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

command -v gh >/dev/null || { echo "gh CLI não encontrado." >&2; exit 2; }
command -v jq >/dev/null || { echo "jq não encontrado." >&2; exit 2; }

APP_ID="${RELEASE_APP_ID:-}"
if [ -z "$APP_ID" ] && [ "$DRY_RUN" -eq 0 ]; then
  echo "RELEASE_APP_ID ausente: informe o ID do GitHub App de release (bypass do ruleset de tags)." >&2
  exit 2
fi
if [ -n "$APP_ID" ] && ! [[ "$APP_ID" =~ ^[0-9]+$ ]]; then
  echo "RELEASE_APP_ID deve ser numérico (recebido: '$APP_ID')." >&2
  exit 2
fi

# Permissão de admin: verificada antes de qualquer escrita.
is_admin="$(gh api "repos/$REPO" --jq '.permissions.admin')"
if [ "$is_admin" != "true" ]; then
  echo "Sem permissão de admin em $REPO: nenhuma alteração aplicada." >&2
  [ "$DRY_RUN" -eq 1 ] || exit 1
  echo "(--dry-run: seguindo apenas para exibir os payloads)" >&2
fi

# send METHOD ENDPOINT JSON: imprime o payload; escreve só fora do --dry-run.
send() {
  local method="$1" endpoint="$2" payload="$3"
  echo "==> $method $endpoint"
  echo "$payload" | jq .
  if [ "$DRY_RUN" -eq 0 ]; then
    echo "$payload" | gh api --method "$method" "$endpoint" --input - >/dev/null
  fi
}

# put_empty ENDPOINT: PUT sem corpo (habilita recursos).
put_empty() {
  echo "==> PUT $1"
  if [ "$DRY_RUN" -eq 0 ]; then
    gh api --method PUT "$1" >/dev/null
  fi
}

apply_ruleset() {
  local file="$1" payload name id
  payload="$(jq -c . "$file")"
  if [ "$(jq -r '.target' <<<"$payload")" = "tag" ] && [ -n "$APP_ID" ]; then
    payload="$(jq -c --argjson id "$APP_ID" '.bypass_actors[0].actor_id = $id' <<<"$payload")"
  elif [ "$(jq -r '.target' <<<"$payload")" = "tag" ]; then
    echo "(--dry-run sem RELEASE_APP_ID: actor_id do bypass fica como 0 no payload)" >&2
  fi
  name="$(jq -r '.name' <<<"$payload")"
  id="$(gh api "repos/$REPO/rulesets" --jq ".[] | select(.name == \"$name\") | .id")"
  if [ -n "$id" ]; then
    send PUT "repos/$REPO/rulesets/$id" "$payload"
  else
    send POST "repos/$REPO/rulesets" "$payload"
  fi
}

SETTINGS="$DIR/repo-settings.json"

send PATCH "repos/$REPO" "$(jq -c '.repository' "$SETTINGS")"
put_empty "repos/$REPO/vulnerability-alerts"
put_empty "repos/$REPO/automated-security-fixes"
put_empty "repos/$REPO/private-vulnerability-reporting"
send PUT "repos/$REPO/actions/permissions/workflow" "$(jq -c '.workflow_permissions' "$SETTINGS")"
send PUT "repos/$REPO/actions/permissions/fork-pr-contributor-approval" \
  "$(jq -c '.fork_pr_contributor_approval' "$SETTINGS")"
apply_ruleset "$DIR/rulesets/main.json"
apply_ruleset "$DIR/rulesets/tags.json"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "Dry-run concluído: nada foi alterado em $REPO."
else
  echo "Configurações aplicadas em $REPO."
fi
