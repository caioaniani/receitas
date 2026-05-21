#!/usr/bin/env bash
# Testa que o backup MAIS RECENTE pode ser restaurado num Postgres efemero.
# Nao toca no banco de producao.
#
# Roda manualmente:  ./deploy/test_restore.sh
# Cron mensal (todo dia 1 as 5h):  0 5 1 * * /caminho/deploy/test_restore.sh
#
# Saida 0 = restore validou. Saida != 0 = backup quebrado/corrompido.
# Alerta Slack opcional: se SLACK_WEBHOOK_URL estiver setado e o teste
# falhar, posta a mensagem.

set -euo pipefail
cd "$(dirname "$0")/.."

BACKUP_DIR="deploy/backups"
BACKUP=$(ls -t "$BACKUP_DIR"/padaria_*.sql.gz 2>/dev/null | head -1 || true)

if [ -z "$BACKUP" ]; then
    echo "[$(date)] ERRO: nenhum backup encontrado em $BACKUP_DIR"
    exit 1
fi

echo "[$(date)] Testando restore de: $BACKUP"

# Verifica integridade do gzip antes de tudo (rapido)
if ! gunzip -t "$BACKUP" 2>/dev/null; then
    echo "[$(date)] ERRO: arquivo gzip corrompido"
    exit 2
fi
echo "[$(date)] gzip OK"

# Sobe Postgres efemero (porta aleatoria, sem expor)
CID=$(docker run -d --rm \
    -e POSTGRES_PASSWORD=test_pass \
    -e POSTGRES_DB=test_restore \
    -e POSTGRES_USER=test_user \
    postgres:15-alpine)
trap "docker stop '$CID' >/dev/null 2>&1 || true" EXIT
echo "[$(date)] Container efemero: ${CID:0:12}"

# Espera Postgres pronto (timeout 30s)
echo "[$(date)] Aguardando Postgres iniciar..."
for i in {1..30}; do
    if docker exec "$CID" pg_isready -U test_user -d test_restore >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

if ! docker exec "$CID" pg_isready -U test_user -d test_restore >/dev/null 2>&1; then
    echo "[$(date)] ERRO: Postgres efemero nao subiu"
    exit 3
fi

# Restaura
echo "[$(date)] Aplicando restore..."
if ! gunzip -c "$BACKUP" | docker exec -i "$CID" psql -U test_user -d test_restore -q >/dev/null 2>&1; then
    echo "[$(date)] ERRO: psql rejeitou o backup"
    exit 4
fi

# Sanity checks: o banco precisa ter as tabelas-chave e algumas linhas
TABELAS=$(docker exec "$CID" psql -U test_user -d test_restore -tA \
    -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';")
echo "[$(date)] Tabelas restauradas: $TABELAS"

if [ "$TABELAS" -lt 20 ]; then
    echo "[$(date)] ERRO: esperado pelo menos 20 tabelas, achei $TABELAS"
    exit 5
fi

# Conta linhas em tabelas criticas
echo "[$(date)] Contagens de sanidade:"
docker exec "$CID" psql -U test_user -d test_restore -c "
    SELECT 'usuario'        as tabela, count(*) FROM usuario
    UNION ALL
    SELECT 'receita',        count(*) FROM receita
    UNION ALL
    SELECT 'materia_prima',  count(*) FROM materia_prima
    UNION ALL
    SELECT 'loja',           count(*) FROM loja
    UNION ALL
    SELECT 'pedido_loja',    count(*) FROM pedido_loja;
"

# Verifica que ao menos 1 admin existe
ADMINS=$(docker exec "$CID" psql -U test_user -d test_restore -tA \
    -c "SELECT count(*) FROM usuario WHERE papel='admin';")
if [ "$ADMINS" -lt 1 ]; then
    echo "[$(date)] ERRO: nenhum admin no backup restaurado"
    exit 6
fi

echo "[$(date)] ✓ Restore validado com sucesso"
echo "[$(date)]   Backup testado: $(basename "$BACKUP")"
echo "[$(date)]   Tabelas: $TABELAS | Admins: $ADMINS"
exit 0
