# Cloudflare Tunnel — guia rápido

## Por que Cloudflare Tunnel?

- **Não precisa abrir portas no router** (Vivo Fibra / Deco / etc.)
- IP residencial pode mudar — o tunnel se reconecta sozinho
- HTTPS automático, sem mexer em certificado
- Esconde o IP da sua casa (privacidade)
- Grátis no plano Free do Cloudflare

## Setup em 5 minutos

### 1. Adicione o domínio ao Cloudflare

Se ainda não fez:

1. Crie conta gratuita em <https://cloudflare.com>
2. **Add a Site** → digite `opaopadariaartesanal.com.br`
3. Escolha o plano **Free**
4. Atualize os nameservers do domínio no registrador (Registro.br) para os do Cloudflare
5. Aguarde o DNS propagar (até 24h, geralmente em 1h)

### 2. Crie o tunnel

1. Acesse <https://one.dash.cloudflare.com>
2. Menu lateral: **Networks → Tunnels**
3. **Create a tunnel** → conector **Cloudflared**
4. Nome: `padaria-casa`
5. **Save tunnel**
6. Na tela seguinte, **copie o token** (`eyJ...`) — você vai precisar dele

### 3. Configure o roteamento público

Ainda na tela do tunnel:

1. **Public Hostnames** → **Add a public hostname**
2. Preencha:
   - Subdomain: `gestao`
   - Domain: `opaopadariaartesanal.com.br`
   - Path: (deixe em branco)
   - Type: `HTTP`
   - URL: `app:5000` (nome do service no docker-compose)
3. **Save hostname**

### 4. Cole o token no .env

```bash
nano .env
```

```
CLOUDFLARE_TUNNEL_TOKEN=eyJhSafe...seu_token_aqui...
```

### 5. Suba o cloudflared

```bash
docker compose --profile tunnel up -d cloudflared
docker compose logs cloudflared
```

Você deve ver algo como:

```
Registered tunnel connection connIndex=0
Registered tunnel connection connIndex=1
Registered tunnel connection connIndex=2
Registered tunnel connection connIndex=3
```

Quatro conexões = redundância. O tunnel está pronto.

### 6. Teste

Abra `https://gestao.opaopadariaartesanal.com.br` no navegador (de outro dispositivo, ou usando dados móveis no celular para não passar pelo Wi-Fi de casa).

Deve abrir o sistema rodando do **seu PC de casa**.

## Adicionar mais serviços (Metabase, Grafana)

Cada subdomínio é uma nova "Public Hostname" no tunnel.

Exemplo para Metabase em `metabase.opaopadariaartesanal.com.br`:

1. No painel do tunnel: **Add a public hostname**
2. Subdomain: `metabase`, Domain: `opaopadariaartesanal.com.br`
3. URL: `metabase:3000` (o service Docker que você criou)
4. Save

Pronto. O mesmo tunnel encaminha múltiplos serviços.

## Troubleshooting

**`docker compose logs cloudflared` mostra `unable to reach the origin`:**
- O service `app` está rodando? `docker compose ps`
- A porta interna está correta? (Flask roda na 5000 dentro do container)

**Token expirou ou inválido:**
- Gere outro no painel Cloudflare → tunnel → **Refresh token**
- Atualize o `.env` e `docker compose restart cloudflared`

**Domínio não resolve:**
- Confirme que os nameservers do Registro.br apontam pro Cloudflare
- `dig gestao.opaopadariaartesanal.com.br` deve retornar IPs do Cloudflare (104.x ou 172.x)

**Velocidade lenta:**
- Cloudflare Tunnel passa por datacenters da Cloudflare — geralmente é mais rápido que conexão direta
- Se estiver lento, pode ser a internet de casa. Teste com `speedtest-cli` no WSL
