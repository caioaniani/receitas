#!/usr/bin/env bash
# Instala/atualiza o Uptime Kuma no VPS. Idempotente: rodar de novo só
# atualiza a imagem e recria o container, preservando os dados (bind mount
# em ./data).
#
#   ./setup.sh                                   # HTTP em :3001
#   ./setup.sh status.opaopadariaartesanal.com.br # HTTPS automático (Caddy)
#
# NÃO mexe em firewall de propósito: este VPS também roda a ponte RADIUS do
# Wi-Fi das lojas (1812/udp). Ligar UFW sem liberar essa porta derrubaria a
# autenticação do Wi-Fi — se for configurar firewall, faça na mão e inclua
# `ufw allow 1812/udp`.
set -euo pipefail

DOMINIO="${1:-}"
DESTINO="${KUMA_DIR:-/opt/uptime-kuma}"

echo "==> Uptime Kuma — instalação em ${DESTINO}"

if [ "$(id -u)" -ne 0 ]; then
	echo "Rode como root (sudo ./setup.sh ...)" >&2
	exit 1
fi

# ── Docker ────────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
	echo "==> Docker não encontrado — instalando (script oficial)"
	curl -fsSL https://get.docker.com | sh
else
	echo "==> Docker já instalado: $(docker --version)"
fi

if ! docker compose version >/dev/null 2>&1; then
	echo "ERRO: 'docker compose' (plugin v2) não disponível." >&2
	echo "No Debian/Ubuntu: apt-get install -y docker-compose-plugin" >&2
	exit 1
fi

# ── Arquivos ──────────────────────────────────────────────────────────
mkdir -p "${DESTINO}/data"
ORIGEM="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for f in docker-compose.yml docker-compose.https.yml Caddyfile; do
	[ -f "${ORIGEM}/${f}" ] && cp "${ORIGEM}/${f}" "${DESTINO}/${f}"
done
cd "${DESTINO}"

# ── Sobe ──────────────────────────────────────────────────────────────
if [ -n "${DOMINIO}" ]; then
	echo "==> Modo HTTPS em ${DOMINIO} (Caddy pede o certificado sozinho)"
	echo "    Confirme que o DNS já aponta pro IP deste VPS, senão o"
	echo "    certificado falha e o Caddy fica reciclando."
	export KUMA_DOMINIO="${DOMINIO}"
	docker compose -f docker-compose.yml -f docker-compose.https.yml pull
	docker compose -f docker-compose.yml -f docker-compose.https.yml up -d
	ENDERECO="https://${DOMINIO}"
else
	echo "==> Modo HTTP na porta 3001 (sem certificado)"
	docker compose pull
	docker compose up -d
	IP="$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || echo '<ip-do-vps>')"
	ENDERECO="http://${IP}:3001"
fi

# ── Espera ficar de pé ────────────────────────────────────────────────
echo -n "==> Aguardando subir"
for _ in $(seq 1 40); do
	if curl -fsS -o /dev/null --max-time 3 http://127.0.0.1:3001 2>/dev/null; then
		echo " OK"
		break
	fi
	echo -n "."
	sleep 3
done
echo

docker compose ps

cat <<EOF

════════════════════════════════════════════════════════════════
 Uptime Kuma no ar: ${ENDERECO}

 1. Abra o endereço e CRIE O USUÁRIO ADMIN (a primeira tela pede;
    até fazer isso, qualquer um que abrir vira o dono).
 2. Cadastre os monitores e o alerta de WhatsApp seguindo o
    README.md desta pasta (seção "Configuração").

 Atualizar depois:  cd ${DESTINO} && ./setup.sh${DOMINIO:+ ${DOMINIO}}
 Backup dos dados:  tar czf kuma-backup.tgz -C ${DESTINO} data
════════════════════════════════════════════════════════════════
EOF
