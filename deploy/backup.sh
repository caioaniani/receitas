#!/usr/bin/env bash
# Backup do Postgres em arquivo .sql.gz compactado.
# Mantém os últimos 30 dias localmente.
# Roda manualmente:  ./deploy/backup.sh
# Roda no cron (todo dia às 3h):  0 3 * * * /caminho/para/deploy/backup.sh
set -euo pipefail

cd "$(dirname "$0")/.."

BACKUP_DIR="deploy/backups"
mkdir -p "$BACKUP_DIR"

DATA=$(date +%Y-%m-%d_%H%M)
ARQUIVO="$BACKUP_DIR/padaria_${DATA}.sql.gz"

echo "[$(date)] Iniciando backup → $ARQUIVO"

# pg_dump dentro do container do postgres (não precisa pg_dump instalado no host)
docker compose exec -T postgres \
    pg_dump -U "${POSTGRES_USER:-padaria}" "${POSTGRES_DB:-padaria}" \
    | gzip > "$ARQUIVO"

TAMANHO=$(du -h "$ARQUIVO" | cut -f1)
echo "[$(date)] Backup completo: $TAMANHO"

# Limpa backups com mais de 30 dias
DELETADOS=$(find "$BACKUP_DIR" -name "padaria_*.sql.gz" -mtime +30 -delete -print | wc -l)
if [ "$DELETADOS" -gt 0 ]; then
    echo "[$(date)] $DELETADOS backup(s) antigo(s) removido(s)"
fi

# Upload offsite pro Dropbox (best-effort — nao falha o backup local se cair).
# Requer DROPBOX_APP_KEY + DROPBOX_APP_SECRET + DROPBOX_REFRESH_TOKEN no env.
# Pasta destino: $DROPBOX_BACKUP_PASTA (default /backups-postgres).
if [ -n "${DROPBOX_APP_KEY:-}${DROPBOX_ACCESS_TOKEN:-}" ]; then
    echo "[$(date)] Subindo pro Dropbox..."
    if python3 deploy/upload_dropbox.py "$ARQUIVO"; then
        echo "[$(date)] Upload Dropbox OK"
    else
        echo "[$(date)] ⚠ Upload Dropbox falhou (backup local preservado)"
    fi
else
    echo "[$(date)] Dropbox nao configurado (pular upload offsite)"
fi

echo "[$(date)] OK"
