# Loja Online — Cutover & Rollback (VNDA → loja própria)

> Estado vivo do corte do VNDA pra loja nativa. Qualquer sessão Claude/dono
> entra aqui pra saber **como virar** e **como voltar** se der ruim.
> Criado em 21/06/2026.

## ⏪ Rollback DNS — voltar pro VNDA

Registro DNS do VNDA **hoje em produção** (painel Wix, zona
`padariaartesanalonline.com.br`). Se o cutover quebrar e for preciso
mandar o tráfego de volta pro VNDA, este é o registro que tem que existir:

| Tipo | Nome do host | Aponta para | TTL |
|---|---|---|---|
| CNAME | `www.padariaartesanalonline.com.br` ⚠️ | `padariaartesanalonline.cdn.vnda.com.br` | 30 minutos |

⚠️ **Host truncado no screenshot** (`www.padariaartesanalonline.…`) —
o valor acima é o provável completo, mas **confirmar no painel Wix antes
de depender dele**. O destino (`padariaartesanalonline.cdn.vnda.com.br`)
está confirmado pelo tooltip.

**Como reverter (se necessário):**
1. No Wix, garantir que o CNAME acima existe/voltou ao valor original.
2. Se `opao.online` tiver sido apontado pro Railway, remover esse apontamento
   (ou deixá-lo, já que o VNDA vive em `padariaartesanalonline.com.br` — os
   dois domínios são independentes).
3. No Railway, setar `LOJA_VISIVEL=0` pra fechar a loja nativa pro público
   (volta a 404 pra anônimo, só admin vê).
4. TTL de 30 min: a propagação reverte em até ~30 min.

> **Nota de arquitetura**: VNDA e loja nativa usam domínios diferentes
> (`padariaartesanalonline.com.br` = VNDA; `opao.online` = loja nativa).
> Manter os dois no ar em paralelo durante o cutover é o plano — o rollback
> é principalmente garantir que o DNS do VNDA não foi tocado e fechar a
> loja nativa via `LOJA_VISIVEL=0`.

## ✅ Checklist de cutover (flip pra público)

| # | Item | Status | Observação |
|---|---|---|---|
| 1 | `SENTRY_DSN` no Railway | ✅ | confirmado pelo dono 21/06 |
| 2 | `LOJA_VISIVEL=1` + `LOJA_HOSTS` | ✅ | confirmado pelo dono 21/06 |
| 3 | Páginas legais (privacidade/termos/trocas/contato) | ✅ | no ar, acessíveis mesmo com loja oculta |
| 4 | `GA4_ID` + `META_PIXEL_ID` no Railway | ⏳ | dono cria contas e seta (sem deploy — lidos de env) |
| 5 | Razão social exata no texto legal | ⏳ | hoje placeholder "O Pão Padaria Artesanal Ltda." em 4 lugares; confirmar com contrato social |
| 6 | DNS swap → Railway | 🟡 em curso | `www.padariaartesanalonline.com.br` apontado e propagado (ver "Estado do DNS" abaixo); apex pendente |
| 7a | DKIM + Return-Path (Postmark) | ✅ | ambos "Verified" no painel 21/06 |
| 7b | **Upgrade Postmark (100 → 10k/mês)** | ⚠️ **bloqueador — confirmar** | grátis = 100 e-mails/mês; 1 pedido completo = até 5 e-mails → ~20 pedidos/mês estoura. Falha é SILENCIOSA (best-effort). US$15/mês resolve. NÃO confundir com Sentry |
| 7c | DMARC `p=none` em `_dmarc.opao.online` | 🔵 opcional | boas-práticas, fase 2. TXT: `v=DMARC1; p=none; rua=mailto:caio@opao.online` |

## 🌐 Estado do DNS (21/06/2026, ~23:20)

**Mudança de plano**: a loja vai viver no domínio do VNDA
(`padariaartesanalonline.com.br`), não só no `opao.online`. Os dois
domínios estão no Railway. Consequência: `LOJA_HOSTS` precisa incluir
AMBOS, senão o domínio fora da lista serve a tela de ADMIN pro público
(`app/__init__.py:330-331`).

| Host | Resolve pra | Status |
|---|---|---|
| `www.padariaartesanalonline.com.br` | Railway (`s8kr0sma.up.railway.app`, IP `69.46.46.90`) | ✅ propagado nos 4 resolvedores públicos |
| `padariaartesanalonline.com.br` (apex, sem www) | `52.21.216.0` (infra antiga, não-Railway) | ❌ **pendente** — apontar/redirecionar pro www |
| `www.opao.online` | Railway | ✅ (verde no painel) |

**Pendências antes de divulgar:**
1. **`LOJA_HOSTS`** deve conter
   `padariaartesanalonline.com.br,www.padariaartesanalonline.com.br`
   além do `opao.online`. **A confirmar** — se faltar, público vê admin.
2. **Apex sem www** ainda na infra antiga — redirect (Wix forwarding)
   `padariaartesanalonline.com.br` → `https://www.padariaartesanalonline.com.br`
   (apex não aceita CNAME).
3. **Painel do Railway pode mostrar "Current: ...vnda..." + "Cloudflare
   detected" mesmo com o DNS já certo** — é cache do checker do Railway
   (TTL 30min do registro antigo). NÃO reverter o `www` por causa disso;
   o valor real já é Railway. Esperar o re-check ficar verde.
4. **Teste de validação**: abrir `www.padariaartesanalonline.com.br` no
   celular em 4G (DNS sem cache local). Loja = OK; tela de admin = falta
   `LOJA_HOSTS`.

## 📧 E-mails por pedido (impacto no teto Postmark)

Um pedido que percorre todo o fluxo dispara até 5 e-mails
(`app/services/email.py`):

1. `enviar_pedido_recebido` — checkout (aguardando pagamento)
2. `enviar_confirmacao_pedido` — webhook pago
3. `enviar_nf_emitida` — após NF emitida
4. `enviar_pedido_a_caminho` — status a_caminho
5. `enviar_pedido_entregue` — status entregue

Mais transacionais de conta: `enviar_reset_senha`,
`enviar_verificacao_cadastro`, `enviar_boas_vindas`.

Domínio verificado no nível do domínio (DKIM domain-wide) → qualquer
`@opao.online` no `From` funciona; a assinatura `noreply@opao.online`
sem "From Name" não é problema.

## 🔧 Chaves de controle (Railway, sem deploy)

| Env var | Efeito |
|---|---|
| `LOJA_VISIVEL` | `1` = loja pública; `0` (default) = 404 pra anônimo, só admin |
| `LOJA_HOSTS` | domínios públicos da loja (ex: `opao.online,www.opao.online`); fora deles a loja só responde pra admin (anti-conteúdo-duplicado) |
| `GA4_ID` | vazio = sem Analytics; setado = GA4 carrega após aceite de cookies |
| `META_PIXEL_ID` | vazio = sem Pixel; setado = Meta Pixel carrega após aceite |
| `SERU_AUTO_SYNC` | `0` desliga TODO o scheduler (inclui o cron que libera reservas de estoque expiradas) — **manter ligado em prod** |

## 🧪 Antes de propagar o DNS

1. Com `LOJA_VISIVEL=1`, fazer **1 pedido real com cartão** + **1 com Pix**.
2. Confirmar e-mail de "recebido" + "confirmado" chegam no inbox (não spam).
3. Conferir baixa de estoque (reserva → consumo) no pedido pago.
4. Conferir NF emitida via Tiny + e-mail com link da DANFE.
5. Só então apontar `opao.online` pro Railway.
