#!/usr/bin/env bash
# Configura o servidor de casa pela primeira vez.
# Executar dentro do WSL2 (Ubuntu) ou Linux nativo.
# Uso: ./deploy/setup-server.sh
set -euo pipefail

echo "════════════════════════════════════════════════"
echo "  Padaria O Pão — setup do servidor de casa"
echo "════════════════════════════════════════════════"
echo

# 1. Verifica dependências
if ! command -v docker &> /dev/null; then
    echo "❌ Docker não está instalado."
    echo "   Instale em: https://docs.docker.com/desktop/install/windows-install/"
    echo "   (Use Docker Desktop com integração WSL2 habilitada)"
    exit 1
fi

if ! docker compose version &> /dev/null; then
    echo "❌ docker compose (v2) não disponível."
    echo "   Atualize o Docker Desktop."
    exit 1
fi

echo "✓ Docker $(docker --version | awk '{print $3}' | tr -d ',')"
echo

# 2. Verifica .env
if [ ! -f .env ]; then
    echo "⚠️  Arquivo .env não encontrado."
    echo "   Copiando .env.example → .env"
    cp .env.example .env
    echo
    echo "   ATENÇÃO: edite o .env com seus valores reais antes de continuar."
    echo "   Especialmente:"
    echo "     - POSTGRES_PASSWORD (gere com: openssl rand -base64 32)"
    echo "     - SECRET_KEY (gere com: python3 -c \"import secrets; print(secrets.token_hex(32))\")"
    echo "     - ANTHROPIC_API_KEY (Claude API)"
    echo "     - SERU_CLIENT_ID / SERU_CLIENT_SECRET"
    echo "     - SLACK_BOT_TOKEN / SLACK_SIGNING_SECRET"
    echo "     - VNDA_API_TOKEN"
    echo
    read -p "Pressione ENTER depois de editar o .env, ou Ctrl+C para sair..."
fi

# 3. Cria diretórios necessários
mkdir -p deploy/backups deploy/logs

# 4. Build inicial
echo
echo "Construindo a imagem Docker (demora ~3-5 min na primeira vez)..."
docker compose build app

# 5. Sobe Postgres primeiro
echo
echo "Subindo Postgres..."
docker compose up -d postgres

echo "Aguardando Postgres ficar pronto..."
for i in {1..30}; do
    if docker compose exec -T postgres pg_isready -U "${POSTGRES_USER:-padaria}" > /dev/null 2>&1; then
        echo "✓ Postgres respondendo"
        break
    fi
    sleep 2
done

# 6. Sobe a aplicação
echo
echo "Subindo a aplicação..."
docker compose up -d app

echo "Aguardando aplicação ficar pronta..."
for i in {1..60}; do
    if curl -fsS http://localhost:5000/health > /dev/null 2>&1; then
        echo "✓ Aplicação respondendo em http://localhost:5000"
        break
    fi
    sleep 2
done

# 7. Próximos passos
echo
echo "════════════════════════════════════════════════"
echo "  Setup completo!"
echo "════════════════════════════════════════════════"
echo
echo "Próximos passos:"
echo "  1. Teste localmente: http://localhost:5000 (login: admin / ${ADMIN_PASSWORD:-admin})"
echo "  2. Importe o banco do Railway: ./deploy/restore.sh <arquivo.sql.gz>"
echo "  3. Configure o Cloudflare Tunnel: veja deploy/cloudflared/README.md"
echo "  4. Configure o backup automático no cron"
echo
echo "Comandos úteis:"
echo "  docker compose logs -f app   # Ver logs em tempo real"
echo "  docker compose ps            # Ver status dos containers"
echo "  docker compose restart app   # Reiniciar só a aplicação"
echo "  ./deploy/backup.sh           # Backup manual do banco"
echo "  ./deploy/deploy.sh           # Pull do git + restart"
echo
