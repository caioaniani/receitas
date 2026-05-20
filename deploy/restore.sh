#!/usr/bin/env bash
# Restaura um backup do Postgres a partir de um arquivo .sql.gz
# Uso: ./deploy/restore.sh deploy/backups/padaria_2026-05-20_0300.sql.gz
#
# ATENÇÃO: substitui TODO o banco atual. Faça backup antes de restaurar.
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Uso: $0 <arquivo.sql.gz>"
    echo "Backups disponíveis:"
    ls -lh deploy/backups/*.sql.gz 2>/dev/null || echo "  (nenhum)"
    exit 1
fi

ARQUIVO="$1"

if [ ! -f "$ARQUIVO" ]; then
    echo "Arquivo não encontrado: $ARQUIVO"
    exit 1
fi

cd "$(dirname "$0")/.."

echo "ATENÇÃO: isso vai SUBSTITUIR todo o banco atual com $ARQUIVO"
read -p "Tem certeza? Digite 'sim' para continuar: " CONFIRMA
if [ "$CONFIRMA" != "sim" ]; then
    echo "Cancelado."
    exit 0
fi

echo "Fazendo backup de segurança do estado atual antes de restaurar..."
SAFETY="deploy/backups/antes_restore_$(date +%Y-%m-%d_%H%M).sql.gz"
docker compose exec -T postgres \
    pg_dump -U "${POSTGRES_USER:-padaria}" "${POSTGRES_DB:-padaria}" | gzip > "$SAFETY"
echo "Backup de segurança salvo em: $SAFETY"

echo "Parando aplicação..."
docker compose stop app

echo "Restaurando $ARQUIVO..."
gunzip -c "$ARQUIVO" | docker compose exec -T postgres \
    psql -U "${POSTGRES_USER:-padaria}" -d "${POSTGRES_DB:-padaria}"

echo "Subindo aplicação..."
docker compose start app

echo "Restore completo. Verifique o sistema."
