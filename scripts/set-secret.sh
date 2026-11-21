#!/usr/bin/env bash
# Add a new version to a Secret Manager secret without the value ever appearing on screen, in
# the shell history or in a file. Usage: scripts/set-secret.sh <secret-id> [project]
# Example: scripts/set-secret.sh lucromei-sentry-dsn
set -euo pipefail
secret="${1:?uso: scripts/set-secret.sh <secret-id> [projeto]}"
project="${2:-lucro-mei}"
gcloud secrets describe "$secret" --project="$project" >/dev/null
printf 'Cole o valor de %s e tecle Enter (não aparece na tela): ' "$secret" >&2
IFS= read -rs value
printf '\n' >&2
[[ -n "$value" ]] || { echo "Valor vazio; nada foi gravado." >&2; exit 1; }
printf '%s' "$value" | gcloud secrets versions add "$secret" --project="$project" --data-file=- \
  --format='value(name)'
unset value
echo "Nova versão gravada. Atualize o número da versão em terraform/main.tf se o segredo for fixado lá." >&2
