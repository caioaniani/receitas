# Atendimento omnichannel via Chatwoot (runbook)

Inbox de WhatsApp + Instagram + Facebook + site, self-hosted, substituindo
Jivochat e EDNA.IO. O sistema da padaria **não** vira inbox — ele integra:
serve o "card do cliente" (histórico de pedidos) dentro do Chatwoot e faz
backup do banco do Chatwoot junto com o seu.

## Divisão de responsabilidade

- **Chatwoot** = conversas, contatos, atribuição a atendente, os 4 canais,
  métricas, app mobile. Dono do dado de atendimento.
- **Sistema Flask (este repo)** = card read-only "o que esse telefone já
  comprou" (`/crm/card`) + backup do Postgres do Chatwoot.
- **Z-API** = alertas internos pro dono. **Não muda nada** (número diferente
  do número de atendimento).

## 1. Subir o Chatwoot no Railway

Projeto separado do sistema da padaria. Serviços: web (Rails) + worker
(Sidekiq) + Postgres + Redis.

**Pegadinhas que NÃO podem passar batido:**

- **Armazenamento de anexos**: disco do Railway é efêmero — foto/áudio do
  WhatsApp somem no redeploy. Configurar **S3-compatível** (Cloudflare R2
  recomendado: barato, sem egress). Env: `ACTIVE_STORAGE_SERVICE=s3` +
  credenciais R2. (Dropbox não serve — ActiveStorage quer S3/GCS/Azure.)
- **SMTP**: convites de atendente e notificações por e-mail precisam de
  `SMTP_*` (Brevo/Resend free tier servem).
- **Domínio**: `atendimento.opaopadariaartesanal.com.br` → `FRONTEND_URL`.
  Necessário pro app mobile e pros webhooks da Meta.
- `SECRET_KEY_BASE`, `RAILS_ENV=production`, `POSTGRES_*`, `REDIS_URL`.

Custo estimado: ~R$150-300/mês.

## 2. WhatsApp (trazer o número que estava na EDNA)

A parceria com a EDNA já foi revogada no Meta Business Manager. Como a WABA
(WhatsApp Business Account) é da sua conta:

1. Meta Business Manager → Business Settings → Users → System Users → criar
   um System User com permissão na WABA + no app.
2. Gerar token permanente. Anotar o **phone number ID** da WABA.
3. Chatwoot → Inbox → API/WhatsApp Cloud → informar phone number ID + token.

**Regra das 24h**: responder cliente que falou nas últimas 24h é livre e
grátis. Iniciar/disparar fora disso exige *template* aprovado pela Meta (custo
por conversa). Disparo em massa que a EDNA fazia = recriar como templates no
Meta Business Manager.

## 3. Instagram + Facebook

Conectar a Página do Facebook + conta Instagram Business no Chatwoot (OAuth).
Exige **Meta App Review** das permissões `instagram_manage_messages` e
`pages_messaging` — submeter cedo (aprovação leva dias). O mesmo Meta App
cobre WhatsApp Cloud + IG + FB.

## 4. Site

Embutir o **widget de site do Chatwoot** (snippet JS) nas páginas do site —
trabalho do time do site.

## 5. Card do cliente (integração com este sistema)

1. Gerar `CHATWOOT_CARD_TOKEN` aleatório:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Setar no Railway do **sistema da padaria** (não do Chatwoot):
   `CHATWOOT_CARD_TOKEN`, `CHATWOOT_URL` (a URL da instância Chatwoot — libera
   o iframe via CSP frame-ancestors).
3. No Chatwoot → Settings → Integrations → **Dashboard Apps** → novo app:
   - URL: `https://gestao.opaopadariaartesanal.com.br/crm/card?k=<TOKEN>`
   - O card aparece na lateral de cada conversa (só na **versão web**; no app
     mobile o atendente vê nome/telefone + a conversa, sem o card).

Match de telefone é canônico BR (ignora +55 e o 9º dígito), então
'5511999998888' casa com um PedidoLocal salvo como '(11) 99999-8888'.

## 6. App mobile + notificações

Os 12 atendentes instalam o **app oficial do Chatwoot** (App Store / Play
Store) e logam apontando pra `FRONTEND_URL`. Push notifications chegam via o
relay do Chatwoot. (Fallback desktop: notificação no navegador com o painel
aberto.)

## 7. Backup + monitoramento

- **Backup do Postgres do Chatwoot**: setar `CHATWOOT_DATABASE_URL` (a URL do
  Postgres do Chatwoot no Railway) no env do **sistema da padaria**. O
  APScheduler já roda um job diário (04:20 BRT) que dumpa esse banco e sobe pro
  Dropbox em `/backups-chatwoot/`. Desligar com `BACKUP_CHATWOOT=0`.
  Implementação: `app/services/seru_cron.py::_run_backup_chatwoot` +
  `app/services/backup.py::executar_backup(db_url=..., prefixo='chatwoot')`.
- **Monitor de uptime**: UptimeRobot (free) apontando pro health do Chatwoot
  (`/` ou `/health`) + a URL de prod do sistema. Alerta se cair.

## 8. Desligar o que sai

Cancelar **Jivochat** só depois dos 4 canais validados em paralelo no
Chatwoot. EDNA já revogada.

## Variáveis de ambiente (resumo, no sistema da padaria)

| Var | Pra quê |
|-----|---------|
| `CHATWOOT_URL` | URL da instância; libera o iframe do card (CSP) |
| `CHATWOOT_CARD_TOKEN` | autentica o iframe do card |
| `CHATWOOT_API_TOKEN` | (futuro) enriquecer atributos do contato |
| `CHATWOOT_ACCOUNT_ID` | (futuro) idem |
| `CHATWOOT_DATABASE_URL` | backup diário do banco do Chatwoot |
