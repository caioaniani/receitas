# deploy/ — servidor de casa

Tudo que é necessário para rodar o sistema no servidor próprio (em paralelo ou substituindo o Railway).

## Arquivos

| Arquivo | Função |
|---|---|
| `setup-server.sh` | Setup inicial — instala e sobe tudo (1ª vez) |
| `deploy.sh` | Pull do git + restart (use no cron) |
| `backup.sh` | Backup do Postgres (use no cron, todo dia 3h) |
| `restore.sh` | Restaurar de um backup `.sql.gz` |
| `cloudflared/README.md` | Como configurar o Cloudflare Tunnel |
| `backups/` | Onde ficam os backups (gitignored) |
| `logs/` | Logs dos scripts (gitignored) |

## Quick reference

```bash
# Setup inicial (1ª vez)
./deploy/setup-server.sh

# Backup manual
./deploy/backup.sh

# Restore
./deploy/restore.sh deploy/backups/padaria_DATA.sql.gz

# Deploy manual
./deploy/deploy.sh

# Cron sugerido
# 0 3 * * *  cd ~/padaria && ./deploy/backup.sh
# * * * * *  cd ~/padaria && ./deploy/deploy.sh
```

## Guia completo

Veja `docs/HOME_SERVER_SETUP.md` para o passo a passo do começo ao fim.
