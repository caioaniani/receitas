# Atendimento omnichannel via Chatwoot (runbook)

## Venda pelo WhatsApp com link de carrinho — 07/10/2026

Decisão do dono: o bot pode atender quem quer COMPRAR, montar o carrinho e
mandar o link do site; o cliente confere, escolhe entrega ou retirada, data
e horário e paga no site. Isso substitui, só para compras, a frase "o robô
não monta carrinho" da política de 24/09 abaixo. O restante continua valendo.

- Entrada: `atendimento_restrito.responder` mantém a FAQ determinística;
  fora dela, sem bloqueio duro, chama `atendimento_venda.responder`.
  Bloqueio duro (sempre equipe): conversa já encaminhada (`handoff_em`),
  anexo ou conteúdo não textual, fala humana da equipe na conversa.
- Travas antes do modelo (`atendimento_venda.motivo_para_equipe`): tentativa
  de manipulação, pedido de atendente, falha de pedido/entrega e reclamação.
- O modelo de vendas só tem quatro ferramentas: `consultar_produtos`
  (catálogo + fatiável, sob encomenda, minis com `pi_id`), `consultar_frete`,
  `montar_link_carrinho` e `transferir_para_humano`. Não consulta pedido,
  nota fiscal nem cadastro, e nunca cria pedido nem cobra.
- Link: `carrinho_link.criar` valida no servidor (publicado, estoque do
  site, fatiado só em sourdough fatiável, composição dos minis com total
  exato, cartinha até 250 caracteres) e grava `CarrinhoLink` (7 dias). A
  composição é guardada pelo mini, não pelo `pi_id`, e sobrevive à edição
  do menu. `/loja/carrinho?link=<token>` revalida tudo, mescla uma vez por
  sessão e pré-preenche a cartinha no checkout; item que saiu ou menu que
  mudou vira aviso visível.
- Link na resposta do bot só se veio de ferramenta (nesta conversa);
  qualquer outro passa a conversa para a equipe.
- Kill-switch `BOT_VENDA=0` (ou sem `ANTHROPIC_API_KEY`): volta ao
  atendimento restrito puro. Custo em `UsoIA` como `bot_atendimento`.
- Testes: `tests/test_venda_whatsapp.py`.

## Política atual: atendimento com a equipe — 24/09/2026

O robô fica limitado a saudações, endereços do cadastro ativo e envio do
site/cardápio quando solicitado explicitamente. Perguntas sobre horário da
loja seguem para a equipe enquanto não houver um cadastro confiável desses
horários. O horário de atendimento do chat mantém a janela já definida.

Pedidos, quantidades, preços, complementos, mensagens de cartão, andamento
de entrega, reclamações, anexos e qualquer solicitação ambígua vão para a
equipe. Por exemplo, **15 lanches + 15 croissants** e **acrescentar um item
ao pedido** abrem atendimento humano com a mensagem original. O robô não
consulta pedidos, confirma entrega, monta carrinho nem encerra a conversa.

A fila é gravada antes das chamadas ao Chatwoot e aparece no painel mesmo
se a comunicação falhar. A recuperação periódica tenta abrir conversas
pendentes em lotes com rodízio; respeita conversas resolvidas ou adiadas.
Depois do encaminhamento, novas mensagens ficam no contexto da equipe,
sem repetir a confirmação. A equipe continua responsável por responder e
fechar o atendimento.

O vigia continua enviando alertas **internos** sobre reclamações e espera.
Não há contenção ou follow-up automático ao cliente. O auditor distingue
os encaminhamentos desta política dos registros antigos e não exige que o
robô retenha vendas. Mensagens humanas, templates da equipe e validação
transacional do portal Wi-Fi permanecem nos seus próprios fluxos.
A validação do Wi-Fi envia o acesso, mas não encerra a conversa: um pedido
que chega junto com o código continua disponível para atendimento.

### Implementação e verificação

- `atendimento_restrito.responder` é determinístico e não chama IA ou tools.
- `chatbot.responder` é a única entrada pública. O motor e follow-up antigos
  têm nomes privados e recusam execução fora de `current_app.testing`.
- Webhook e recuperação usam a mesma fila durável e os locks por conversa.
- O gateway exige a política interna para qualquer fala automática; a
  confirmação inicial tem finalidade própria. Dados do cliente não definem
  esses campos.
- Não há alteração de schema. Testes cobrem compras, complemento, entrega,
  anexos, supervisão, indisponibilidade da rede e concorrência com a equipe.
- Testes históricos do motor anterior são avaliações offline; os testes
  `test_atendimento_restrito*` verificam a política que atende clientes.

### Mensagem que o WhatsApp não entregou ("This message is unavailable.")

Quando a Meta entrega um evento como `type: unsupported`, o Chatwoot grava
só um placeholder — "This message is unavailable." — com
`content_attributes.is_unsupported`. O tipo original da mensagem não chega
no evento (o Chatwoot só loga o título do erro e o contato, quando o array
`errors` vem). Duas origens documentadas: formato que a API não entrega
(erro 131051 — enquete, visualização única, mensagem apagada) e número em
coexistência com o aplicativo WhatsApp Business (erro 131060 — a mensagem
pode estar visível no aparelho). Mensagem temporária e reação não geram o
placeholder: o Chatwoot as descarta antes. O Chatwoot não recebe o
conteúdo — nem o bot, nem a equipe pelo painel. O sistema reconhece o
placeholder (`chatwoot.mensagem_indisponivel`) e:

- responde ao cliente, uma vez por episódio, que a mensagem chegou sem
  conteúdo e pede o reenvio como texto comum — sem diagnosticar a causa;
- encaminha a conversa à equipe com o marcador `[mensagem indisponível…]`
  no lugar do texto em inglês e o motivo (com as causas conhecidas) na
  nota privada;
- mostra o mesmo marcador na fila de espera, na lista de conversas do
  painel e nos avisos ao dono (o marcador cabe nos 120 caracteres do
  aviso); o histórico antigo gravado com o texto em inglês é lido já com
  o marcador.

O que a equipe faz: esperar o reenvio; se o número da inbox também estiver
no aplicativo WhatsApp Business (coexistência), conferir a mensagem no
aparelho; se o número for conhecido, ligar. Não adianta responder "pode
escrever?" — o cliente provavelmente já escreveu. Foto sem legenda que
chega na mesma rajada de um placeholder vai para a equipe como anexo (a
foto chegou). Caso de referência: conversa 2339 (15 a 29/09/2026), sete
mensagens do mesmo contato sem conteúdo.

As seções de implantação abaixo descrevem a infraestrutura. Orientações
anteriores de autonomia do robô ficam substituídas por esta política.

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

### Interrupções na consulta de conversas (10/09/2026)

Os eventos Sentry GESTAO-PADARIA-4P e 4Q ocorreram às 06:12:58/59:
`No route to host` na lista aberta e corpo HTTP interrompido na lista pending.
As leituras dessas duas listas fazem até duas tentativas, com 0,5 s entre
elas e timeouts de conexão/leitura de 3/5 s. Retry restrito a falhas de
conexão, timeout, corpo interrompido e HTTP 502/503/504; não se aplica a
POSTs nem a credenciais recusadas. Falhas definitivas continuam no Sentry.

As duas APIs do painel usam `estrito=True` e devolvem HTTP 503 com aviso
em caso de falha. A lista mantém os dados anteriores; o indicador informa
que não foi possível atualizar. O polling de pending ignora a consulta
malsucedida e só estabelece a base do alarme após uma resposta válida.
Consumidores antigos mantêm o contrato de lista vazia em falha.

Validação: `tests/test_chatwoot_resiliencia.py` e `tests/test_painel_testes.py`.
A causa da indisponibilidade de rede no servidor não é determinada apenas
pelos dois eventos; esta mudança trata a recuperação e a apresentação da falha.
