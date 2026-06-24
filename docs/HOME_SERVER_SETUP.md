# Setup do servidor de casa

Guia passo a passo para migrar a Padaria O Pão do Railway para o seu PC Windows.

## Visão geral

```
Internet → Cloudflare → Cloudflare Tunnel → Seu PC (Vivo Fibra)
                                              ↓
                                            WSL2 Ubuntu
                                              ↓
                                            Docker Compose
                                              ├─ Flask (gunicorn)
                                              ├─ Postgres 16
                                              └─ Cloudflared
```

**O que você ganha:**
- Custo mensal próximo de zero (só energia)
- Deploys em segundos em vez de minutos
- Controle total dos dados
- Possibilidade de instalar Metabase, Grafana, n8n, etc. sem custo extra

**O que você precisa ter pronto:**
- PC Windows ligado 24/7
- Vivo Fibra (ou similar)
- Conta no Cloudflare (gratuita) com o domínio `opaopadariaartesanal.com.br` apontado para lá
- Acesso ao Railway atual para fazer o backup do banco antes de migrar

---

## Etapa 1 — Instalar WSL2 no Windows

Abra o **PowerShell como Administrador** e rode:

```powershell
wsl --install -d Ubuntu-24.04
```

Reinicie o PC quando pedir. Depois do reboot, o Ubuntu abre e pede para criar usuário/senha — escolha algo que você lembre.

**Confirme:**

```bash
lsb_release -a
# Deve mostrar Ubuntu 24.04
```

---

## Etapa 2 — Instalar Docker Desktop

1. Baixe em: <https://docs.docker.com/desktop/install/windows-install/>
2. Instale com o instalador
3. Abra o Docker Desktop
4. Vá em **Settings → Resources → WSL integration**
5. Habilite a integração com Ubuntu-24.04
6. Apply & Restart

**Confirme dentro do WSL Ubuntu:**

```bash
docker --version
docker compose version
```

---

## Etapa 3 — Clonar o repositório

Dentro do WSL Ubuntu:

```bash
cd ~
git clone https://github.com/caioaniani/receitas.git padaria
cd padaria
git checkout claude/continue-controller-conversation-aGS3F
```

---

## Etapa 4 — Configurar variáveis de ambiente

```bash
cp .env.example .env
nano .env  # ou code .env se preferir VS Code
```

Preencha **no mínimo:**

| Variável | Como obter |
|---|---|
| `POSTGRES_PASSWORD` | `openssl rand -base64 32` (gera senha forte) |
| `SECRET_KEY` | `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `ADMIN_PASSWORD` | Inventa uma senha forte |
| `ANTHROPIC_API_KEY` | Pega do painel Railway → variáveis |
| `SERU_CLIENT_ID` / `SECRET` | Pega do Railway |
| `VNDA_API_TOKEN` | Pega do Railway |
| `SLACK_BOT_TOKEN` / `SIGNING_SECRET` | Pega do Railway |

Salve com Ctrl+O, Enter, Ctrl+X.

---

## Etapa 5 — Backup do banco do Railway

**No painel do Railway:**

1. Abra o serviço **Postgres**
2. Vá em **Variables**
3. Copie a `DATABASE_URL` (ou os valores `PGHOST`, `PGUSER`, etc.)

**No WSL Ubuntu:**

```bash
# Instala o cliente do Postgres
sudo apt update && sudo apt install -y postgresql-client

# Faz o dump (substitua a URL pela DATABASE_URL do Railway)
pg_dump "postgresql://USUARIO:SENHA@HOST:PORTA/BANCO" \
    | gzip > backup_railway.sql.gz

# Confirma que tem dados
gunzip -c backup_railway.sql.gz | head -20
```

Guarde esse `backup_railway.sql.gz` — vamos restaurar no servidor local.

---

## Etapa 6 — Subir o servidor local

```bash
cd ~/padaria
./deploy/setup-server.sh
```

O script vai:
1. Validar que o Docker funciona
2. Construir a imagem
3. Subir o Postgres
4. Subir a aplicação
5. Testar o `/health`

**Confirme que está vivo:**

```bash
curl http://localhost:5000/health
# Deve responder: ok
```

Abra no browser: <http://localhost:5000> — login `admin` com a senha que você definiu em `ADMIN_PASSWORD`.

---

## Etapa 7 — Restaurar o banco do Railway

```bash
mv backup_railway.sql.gz deploy/backups/
./deploy/restore.sh deploy/backups/backup_railway.sql.gz
```

Aguarde alguns segundos e recarregue a página `localhost:5000`. Agora deve aparecer com os dados reais (receitas, pedidos, vendas, etc.).

---

## Etapa 8 — Cloudflare Tunnel

O Cloudflare Tunnel expõe seu servidor para a internet **sem abrir portas no router**.

### 8.1. Criar o tunnel

1. Acesse <https://one.dash.cloudflare.com>
2. Em **Networks → Tunnels → Create a tunnel**
3. Escolha **Cloudflared** como connector
4. Dê um nome: `padaria-casa`
5. Copie o **token** que aparece (`eyJ...`) e cole em `.env`:

```bash
CLOUDFLARE_TUNNEL_TOKEN=eyJ...cole_aqui...
```

### 8.2. Configurar o roteamento

Ainda no painel do Cloudflare:

1. Public hostnames → **Add a public hostname**
2. Subdomain: `gestao`
3. Domain: `opaopadariaartesanal.com.br`
4. Service: `http://app:5000`
5. Salvar

### 8.3. Subir o tunnel

```bash
docker compose --profile tunnel up -d cloudflared
```

Confirme:

```bash
docker compose logs cloudflared
# Deve mostrar "Registered tunnel connection" 4x (4 datacenters)
```

### 8.4. Testar

Acesse `https://gestao.opaopadariaartesanal.com.br` no navegador. Deve abrir o sistema **do seu PC de casa**.

---

## Etapa 9 — Cutover do DNS

Esse é o passo de "virar a chave" — agora o domínio aponta pro seu PC, não mais pro Railway.

1. No Cloudflare DNS, garanta que o registro CNAME do `gestao` aponta para o tunnel (`<tunnel-id>.cfargotunnel.com`) — o painel faz isso automaticamente quando você cria a public hostname.
2. Espere 1-2 minutos para o DNS propagar
3. Teste de outro dispositivo (celular com 4G, não Wi-Fi de casa)

---

## Etapa 10 — Auto-start ao ligar o PC

**No Windows, configure o Docker Desktop para iniciar com o sistema:**

1. Docker Desktop → Settings → General
2. Marque "Start Docker Desktop when you log in"

**No WSL Ubuntu, crie um script que sobe os containers ao logar:**

```bash
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/padaria.sh << 'EOF'
#!/bin/bash
sleep 30  # espera o Docker estar pronto
cd ~/padaria
docker compose --profile tunnel up -d
EOF
chmod +x ~/.config/autostart/padaria.sh
```

---

## Etapa 11 — Backup automático

Configure o cron para fazer backup todo dia às 3h da manhã:

```bash
crontab -e
```

Adicione a linha:

```
0 3 * * * cd ~/padaria && ./deploy/backup.sh >> deploy/logs/backup.log 2>&1
```

**Opcional — backup offsite (recomendado):**

Use o Backblaze B2 (~R$ 5/mês para muitos GB) com o `rclone`:

```bash
sudo apt install rclone
rclone config  # configura o backend Backblaze B2
```

Descomente a linha do upload em `deploy/backup.sh`.

---

## Etapa 12 — Auto-deploy via git

Configure o cron para checar commits novos a cada 1 minuto:

```bash
crontab -e
```

Adicione:

```
* * * * * cd ~/padaria && ./deploy/deploy.sh >> deploy/logs/cron.log 2>&1
```

Agora, quando você fizer `git push` para o GitHub, em até 1 minuto o servidor de casa já está atualizado.

---

## Comandos úteis no dia a dia

```bash
cd ~/padaria

# Ver logs em tempo real
docker compose logs -f app

# Status dos containers
docker compose ps

# Reiniciar só a app (sem mexer no banco)
docker compose restart app

# Backup manual
./deploy/backup.sh

# Restaurar de backup
./deploy/restore.sh deploy/backups/padaria_2026-05-20_0300.sql.gz

# Atualizar manualmente
./deploy/deploy.sh

# Parar tudo (banco continua salvo no volume)
docker compose down

# Subir tudo
docker compose --profile tunnel up -d

# Rebuild da imagem (depois de mudança em Dockerfile/requirements)
docker compose build app && docker compose up -d app

# Acessar o Postgres via psql
docker compose exec postgres psql -U padaria

# Limpar logs antigos
find deploy/logs -mtime +30 -delete
```

---

## Troubleshooting

**O site não abre depois do tunnel:**
- `docker compose logs cloudflared` — vê erros
- Confirme que o tunnel está registrado: `https://one.dash.cloudflare.com → Tunnels`
- O hostname está apontando para `http://app:5000`?

**Erro `cannot connect to postgres`:**
- `docker compose ps` — Postgres está rodando?
- `docker compose logs postgres` — algum erro?
- Senha no `.env` bate com o que o container subiu? Se mudou, precisa apagar o volume (`docker compose down -v` — CUIDADO, apaga os dados)

**Memória estourando:**
- `docker stats` — mostra consumo por container
- Aumente o `WSL2 memory limit` em `~/.wslconfig`:
  ```
  [wsl2]
  memory=8GB
  processors=4
  ```

**Deploys parando de funcionar:**
- `cat deploy/logs/deploy_*.log` — vê o que aconteceu
- `git status` — alguma alteração local conflitando?
- `git fetch && git status` — está atrasado?

**PC reiniciou sozinho (Windows Update):**
- Settings → Windows Update → Advanced options → Pause updates por 5 semanas
- Configure "Active hours" para o Windows não reiniciar de madrugada
