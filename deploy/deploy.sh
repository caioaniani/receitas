#!/usr/bin/env bash
# Auto-deploy: puxa o último commit do GitHub e reinicia a aplicação.
# Pode rodar:
#   - Manualmente: ./deploy/deploy.sh
#   - Via webhook do GitHub (configurar webhook.sh)
#   - Via cron (a cada 1min checa se tem commit novo)
set -euo pipefail

cd "$(dirname "$0")/.."

LOG_DIR="deploy/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/deploy_$(date +%Y-%m-%d).log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"
}

log "=== Deploy iniciado ==="

# Pega o branch atual (qualquer um, mas geralmente claude/continue-controller-conversation-aGS3F)
BRANCH=$(git rev-parse --abbrev-ref HEAD)
log "Branch: $BRANCH"

# Verifica se há commits novos no remote
git fetch origin "$BRANCH" >> "$LOG" 2>&1
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
    log "Já está atualizado (commit $LOCAL). Saindo."
    exit 0
fi

log "Atualizando: $LOCAL → $REMOTE"
git pull origin "$BRANCH" >> "$LOG" 2>&1

# Verifica se requirements.txt mudou — se sim, rebuilda imagem
if git diff "$LOCAL" "$REMOTE" --name-only | grep -q "^requirements.txt$\|^Dockerfile$"; then
    log "requirements.txt ou Dockerfile mudou → rebuild completo"
    docker compose build app >> "$LOG" 2>&1
fi

log "Reiniciando aplicação..."
docker compose up -d app >> "$LOG" 2>&1

# Aguarda healthcheck
log "Aguardando healthcheck..."
for i in {1..30}; do
    if curl -fsS http://localhost:5000/health > /dev/null 2>&1; then
        log "Aplicação respondendo em http://localhost:5000/health"
        log "=== Deploy completo ==="
        exit 0
    fi
    sleep 2
done

log "ERRO: aplicação não respondeu após 60s. Verifique: docker compose logs app"
exit 1
