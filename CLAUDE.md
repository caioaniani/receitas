# Convenções de trabalho (Claude)

## Tom e linguagem

- **SEMPRE escreva em português correto, independentemente de como o usuário
  escreve.** Se o usuário escreve informal ou com erros, isso NÃO é autorização
  para escrever errado de volta. Ele digita rápido sem corrigir; eu não tenho
  essa desculpa.
- **NUNCA use "cê", "vc", "tá", "pra" em contextos sérios.** Escreva "você",
  "está", "para".
- Sem abreviações coloquiais. Sem gírias.
- Linguagem direta e técnica, mas sempre respeitosa. Sem emojis a menos que o
  usuário use primeiro.

## Versão canônica, nunca o atalho

Esta regra existe porque eu (Claude) caí na tentação de "pragmático" varias
vezes em areas de risco (dinheiro, estoque, hooks de seguranca) e o usuario
identificou o padrao. Em **toda** decisao de implementacao:

- **Default = versao canonica/correta**, mesmo que seja mais cara
  (migration, refactor maior, mais arquivos).
- **NUNCA escolher unilateralmente "pragmatico/menos invasivo"** quando ha
  trade-off de correcao. Se a versao canonica eh muito cara, **PERGUNTAR
  explicitamente o trade-off ao usuario** antes de cortar caminho.
- **NUNCA silenciar erros** com `|| true`, `2>/dev/null`, `--quiet`,
  `# noqa` sem justificativa documentada no codigo. Lint/test/hook que
  acusa problema tem que aparecer pro usuario.
- **Dinheiro e estoque tem peso especial.** Qualquer mudanca em
  `VendaB2B*`, `EstoqueLoja`, `EstoqueProducao`, `Mov*Estoque*` exige
  versao canonica. Se vier tentacao de "tolerancia de centavos", "filtro
  com `1e-9`", "aceitar como erro conhecido" — esta errado, perguntar
  primeiro.

Exemplos de violacoes recentes (2026-05-21, todos identificados pelo
usuario, nao por mim espontaneamente):
- Auto-commit hook com `ruff check --fix --quiet 2>/dev/null || true`
  silenciava erros nao-fixaveis. Corrigido pra abortar commit.
- B4 (Decimal pra dinheiro) feito com tolerancia de 1 centavo em vez
  de migrar colunas pra `Numeric(10,2)`. Refeito da forma canonica.
- B9 (fracao inestornavel) marcado como "aceitar" sem perguntar.
  Refeito com `SeruDebitoMov` proper.
- 2026-05-22: adicionei colunas `modificado_em`/`modificado_por_id` em
  `PedidoLoja` direto no modelo, sem ALTER explicito em
  `_migrate_postgres()`. Auto-commit pushou pra prod, Railway deployou,
  Postgres nao tinha as colunas, qualquer `SELECT pedido_loja.*` virou
  500. Causa raiz dupla: (1) confiei no CLAUDE.md que afirmava
  "Railway aplica Alembic automaticamente" sem verificar (era falso —
  ver "Schema migrations" abaixo, ja corrigido), (2) nao auditei o
  caminho de aplicacao de schema antes de mudar modelo. Procedimento
  correto: commit 1 = ALTER no helper legado + push + aguardar deploy;
  commit 2 = modelo + logica.

## Verificação antes de afirmar

Esta regra é obrigatória e se aplica a TODA conversa.

- **Antes de afirmar que um trecho do código tem ou não tem um problema,
  verifique com `grep`/`Read` o arquivo real.** Hipóteses ("e se isso estivesse
  errado") são úteis pra raciocinar, mas precisam ser marcadas explicitamente
  como hipótese e validadas antes de virar afirmação ou recomendação.
- **Toda afirmação técnica deve vir acompanhada de `arquivo:linha`**, exceto
  quando é claramente teórica/conceitual. Sem citação = é suposição, e deve
  ser dita como tal.
- **Quando o usuário pedir uma solução pra um problema específico, audite
  primeiro o código que toca o ponto, depois proponha.** Liste o que achou
  com `arquivo:linha` antes da proposta. O usuário não consegue distinguir
  "Claude analisou e achou X" de "Claude supôs que talvez exista X" — então
  sempre deixe claro qual dos dois é.
- **Quando duas implementações divergem** (constantes duplicadas, filtros
  copiados, lógica replicada), tratar como dívida real: ou centralizar
  (`app/constants.py`, helpers em `app/utils.py`), ou explicar por que a
  duplicação é intencional. Não fingir que não viu.
- **Se notar um bug enquanto resolve outra coisa, mencione-o.** O usuário
  pediu A; se você achou B no caminho, é responsabilidade reportar B. Cada
  vez que isso falha (eu menciono risco hipotético em vez de ir conferir o
  código real), é um problema concreto, não estilo.

## Fluxo multi-agente (só para tarefas complexas)

Use este fluxo quando a tarefa for complexa: feature nova, refactor, mudança
que toca 3+ arquivos ou exige investigar várias partes do sistema. Para
correções pontuais (1–2 arquivos), trabalhe direto — subagentes aí só
adicionam custo e latência.

1. **Especificação completa primeiro.** Antes de delegar, consolide objetivo,
   restrições e critério de "pronto". Se algo essencial estiver ambíguo,
   pergunte antes de começar.
2. **Cascateie a investigação em subagentes paralelos.** Quebre a tarefa em
   subtarefas de leitura/pesquisa independentes e lance os subagentes
   (Explore / general-purpose) **numa única mensagem, em paralelo**. Cada um
   devolve só conclusões com `arquivo:linha` (arquivos relevantes, como o
   fluxo funciona, riscos) — nunca despejo de código.
3. **Consolide e execute você mesmo.** Subagentes não compartilham contexto
   entre si nem com você; quem escreve o código é o orquestrador, com base no
   que voltou. Não delegue a escrita de código que precise de visão do todo.
4. **Revisão independente.** Ao terminar a implementação, lance o subagente
   `revisor` (`.claude/agents/revisor.md`) para criticar o diff com contexto
   limpo. Ele reporta **todos** os achados com confiança e severidade; a
   filtragem é do orquestrador, não dele.
5. **Avalie e itere — no máximo 2 rodadas.** Corrija o que for procedente
   (re-delegando investigação a subagentes se necessário) e rode a revisão de
   novo. Itere só por bug, caso de borda ou violação das convenções deste
   arquivo — nunca por preferência de estilo.
6. **Entregue.** Rode a suíte (`python -m pytest`, ~73s) e exercite o fluxo
   afetado, e resuma: o que mudou, o que a revisão apontou e o que foi
   corrigido ou descartado (e por quê).

## API read-only do assistente (/api/claude/*) — acesso do Claude a prod

Criada em 02/07/2026 a pedido do dono ("pois vá ter acesso"): o container de
desenvolvimento NAO enxerga o Postgres do Railway, entao leituras de producao
saem por HTTPS com token. Blueprint `app/blueprints/claude_api/`.

- **Auth**: `Authorization: Bearer <CLAUDE_API_TOKEN>` (env no Railway; vazio
  = rotas desligadas com 503). Mesmo padrao do `BOT_API_TOKEN`.
- **Rotas** (read-only estrito, nunca adicionar write aqui):
  - `GET /api/claude/cronograma?horizonte=&janela=&inicio=` — JSON do
    cronograma de producao (mesma conta da /telaindustriateste, com
    pendencias e alertas de entrega em risco).
  - `GET /api/claude/pedidos-semana?modo=venda|media&...` — sugestao de
    pedido loja→industria (mesma conta das telas Pedidos da semana).
  - `GET /api/claude/receita?id=|nome=` — ficha completa de uma receita
    (cadastro, ingredientes, VendaMapa, cestas, estoques industria+lojas).
    Trecho de nome com >1 match devolve lista de candidatos.
- **Uso numa sessao**: o dono cola o token no chat (o container e efemero —
  nada persiste entre sessoes); consultar com
  `curl -s -H "Authorization: Bearer $TOK" https://gestao.opaopadariaartesanal.com.br/api/claude/cronograma`.
- Testes: `tests/test_claude_api.py`.

## Branches & Deploy

- **Branch de produção (Railway acompanha)**: `claude/continue-controller-conversation-aGS3F`
- **URL publica de prod**: https://gestao.opaopadariaartesanal.com.br/
- **Auto-deploy** no Railway (Auto deploys ON, **Wait for CI ON** — religado pelo
  usuario em 2026-06-09 apos a janela de fix de NF). CONSEQUENCIA: cada push
  **espera o CI passar** antes de subir. Com o CI agora em ~1,5 min (ver abaixo),
  o deploy gira em ~3-5 min (CI + build Docker). Pra deploy rapido em emergencia:
  desligar "Wait for CI" no Railway temporariamente.

- **CI rapido (refatorado 2026-06-09)**: a suite caiu de **~12 min pra ~73s**
  (~10x). O `tests/conftest.py` cria o app + schema UMA vez por sessao e reseta
  entre testes via `DELETE` de linhas (~0.02s/teste vs 0.88s do drop+create).
  Dois cuidados que o app-compartilhado exigiu (ja resolvidos no conftest):
  - **Rate limiter**: `limiter.reset()` no inicio de cada teste (sem isso, o
    estado acumula entre testes e estoura 429 no login).
  - **Indices de migration**: dropar indices que NAO sao do modelo no inicio de
    cada teste (ex: `uq_estoque_loja_receita` de `_migrate_estoque_trava`) — eles
    vazavam entre testes e quebravam os que criam duplicatas de proposito.
  - **Config**: snapshot/restore por teste (mutacoes `app.config[X]=Y` nao vazam).
  - xdist ainda dormente (`PYTEST_XDIST_WORKER` da SQLite proprio por worker);
    com 73s sequencial nao foi preciso paralelizar.
- **Workflow**: **SEMPRE commit direto no branch de producao**. Nao abrir PR — o auto-commit
  hook ja faz commit+push pro branch atual, e o usuario nao quer mergear nada manualmente.
  Se a mudanca for grande, ainda assim vai direto em prod (auto-commit acumula varios commits).
- **Nunca** force-push nem `--no-verify` sem autorização explícita.

### Railway usa Dockerfile (nao Nixpacks)

**ATENCAO**: o `railway.json` diz `"builder": "NIXPACKS"`, mas Railway IGNORA
isso e usa o `Dockerfile` na raiz (detectou e priorizou). Confirmado em
2026-05-22 ao debugar `pg_dump nao encontrado` — `/nix/store/` vazio,
`/etc/os-release` retorna Debian trixie, `/app` com owner `padaria` (bate
com `USER padaria` do Dockerfile).

**Consequencias**:
- `nixpacks.toml` na raiz **e ignorado**. Nao adicione.
- Env var `NIXPACKS_APT_PKGS` no Railway **e ignorada**.
- Pra instalar pacotes apt em prod, **edite o Dockerfile** (linha do
  `RUN apt-get install`).
- Pacotes Postgres precisam vir do repo pgdg (`apt.postgresql.org`),
  nao do Debian main — Debian 13 (trixie) so tem ate `postgresql-client-17`
  e o server Railway eh PG 18. Padrao: ver bloco do `Dockerfile` que
  adiciona o keyring GPG + sources.list.d antes de instalar
  `postgresql-client-18`.

**Rota debug**: `/admin/backup/debug-env` (owner-only) mostra PATH,
`which pg_dump`, conteudo de `/`, `/etc/os-release`, e pacotes apt
relevantes. Util quando suspeitar que algo nao subiu no build.

## Auto-commit hook ativo

`.claude/scripts/auto-commit.sh` + `.claude/settings.json` configurados:
PostToolUse no Write|Edit faz `git add <file>` + commit "auto: update X" + push pro branch
atual. Em sessao nova, o hook ja esta ativo no startup.

**Risco zero em prod**: o hook so dispara via PostToolUse do Claude Code CLI.
Railway/gunicorn nunca chama esses scripts — sao especificos do ambiente de
desenvolvimento com Claude Code. O `.claude/` esta commitado pra outras sessoes
de desenvolvimento manterem o mesmo comportamento, mas o arquivo `.sh` em prod
fica dormente (nada o invoca).

**Limitacao conhecida**: o hook so pega arquivos modificados via Write/Edit do
Claude. Comandos shell que escrevem direto (ex: `ruff --fix`, `flask db init`,
ou um pip install que cria arquivos) precisam de `git add` + commit manual.
O Stop hook global avisa quando isso acontece (saida nao commitada).

**Armadilha do push silencioso** (2026-06-09): se o clone local estiver ATRAS
do remoto (ex: container novo clonou um commit velho), o push do hook falha
SILENCIOSAMENTE a sessao inteira — os commits ficam so locais e a suite roda
contra codigo desatualizado (sintoma: testes subitamente lentos ou arquivo
"sumido"). Antes de confiar no estado local: `git fetch` + `git rev-list
--left-right --count <branch>...origin/<branch>`. Se divergiu, rebase em cima
do origin (os commits locais nunca foram pushados — rebase sem force e ok).

## CI / GitHub Actions

`.github/workflows/ci.yml` roda `ruff check` + `pytest` em cada push.
Tres armadilhas que ja me pegaram:

1. **`python -m pytest`, nao `pytest` direto**. `pytest` puro nao adiciona
   o cwd ao `sys.path` — o CI da `ModuleNotFoundError: No module named 'app'`.
   `python -m pytest` adiciona. Localmente sempre rodei via `python -m`,
   entao escapou na primeira validacao.

2. **Rodar `ruff check app/ tests/` antes de cada push grande**. O `--fix`
   so passa uma vez; se eu modifico arquivo via Edit depois, posso
   introduzir novo problema. Antes de fechar uma rodada de mudancas,
   rodar `ruff check` mesmo se "tudo parece OK".

3. **Validar workflow novo localmente com a mesma sequencia exata**.
   Quando adicionei o CI, nao tinha rodado `pytest` direto (sem `python -m`)
   no shell — passou na minha cabeca, falhou no GitHub. Pra workflow novo,
   reproduzir cada step do YAML em sequencia antes de subir.

## Pendentes da auditoria (2026-05-21)

Auditoria continuada (estoque/dinheiro) — TODOS fechados nesta sessao
(2026-05-21):

- ✓ **B4 — Decimal pra dinheiro.** Colunas `valor_total`, `valor`,
  `valor_pago`, `preco_unitario`, `valor_unitario` migradas pra
  `Numeric(10, 2)` via Alembic (`643bd66e89c3`). Properties e service
  usam `Decimal`. Sem tolerancia hack — precisao exata.

- ✓ **B5 — FK em ProdutoItem.** Colunas `receita_id` e `materia_prima_id`
  adicionadas em `ProdutoItem` via migration `efb6e5837fd0`. Backfill
  por nome exato no `upgrade()`. Orfaos (sem FK) sao logados WARNING +
  contados no dashboard do owner com link `/cestas/orfaos` pra
  vincular manualmente. `item_nome` mantido por compat — usado apenas
  como fallback humano-legivel quando FK eh NULL.

- ✓ **B9 — Fracao inestornavel.** Modelo `SeruDebitoMov` registra
  cada contribuicao por pedido. `_estornar_pedido` reverte fracao:
  se acumulador fica negativo, devolve inteiros ao estoque. Migration
  `ac57b6648ec4`.

Fechado 2026-05-22:

- ✓ **Audit log de edicao de pedido.** Colunas `modificado_em` /
  `modificado_por_id` em `PedidoLoja` (ALTER em
  `_migrate_postgres()`). Setadas na rota web (`/pedidos/<id>/editar`)
  e no executor copilot (`editar_pedido`). AuditLog automatico
  (`app/services/audit.py`) ja capturava as mudancas; fix em
  `_current_user_id()` pra ler `db.session.info['audit_user_id']` —
  caminho Slack/async sem Flask-Login. Handler Slack em
  `slack_bot.py:438` seta isso antes do `executar`. Filtro
  `registro_id` no `/audit`. Link "historico completo" no detalhe do
  pedido (so admin/gerente).

Da auditoria 1, ainda pendentes:

- **M6 — Mover BLOBs pro Dropbox** (parcial, 4 de 6 migrados em 22/05/2026):
  - ✅ Migrados pra Dropbox: `Receita.imagem_blob`, `Produto.imagem_blob`,
    `FotoRecebimento.imagem`, `PedidoItemFoto.imagem`, `EntregaFoto.imagem`.
  - ✗ Mantidos BLOB no Postgres por seguranca (PII):
    `Atestado.arquivo` (atestado medico), `Loja.planta_imagem`.
  - ⏳ **Pendente Commit D**: dropar as colunas BLOB ja-vazias dos 4
    modelos migrados. Hoje todas as linhas tem `imagem*=NULL` e
    `imagem_url/imagem_dropbox_url` preenchido. Drop libera espaco
    de disco. Padrao por modelo: `ALTER TABLE <t> DROP COLUMN IF EXISTS
    <coluna>` em `_migrate_postgres()` + remover do modelo + remover
    fallback BLOB nas serve routes (`cardapio_img`, `pedidos.foto`,
    `handshake.foto_serve`) + atualizar `_render_fotos` no
    `app/services/relatorio.py` (que ja prioriza URL).
  - Backfill rotas em `/admin/debug-schema` (card "Migracao BLOB").
    Idempotentes — podem ser re-rodadas a qualquer momento.
  - Servico: `app/services/blob_migrator.py`. Helper compressao:
    `app.utils.comprimir_imagem(bytes, max_size=700, quality=82)`.
  - URL Dropbox usa `?raw=1` (CDN raw bytes), nao `?dl=0` (preview HTML).
    `dropbox_storage._converter_para_raw()` normaliza via `urllib.parse`.
  - **CSP**: `img-src` precisa incluir `https://*.dropbox.com` e
    `https://*.dropboxusercontent.com` (corrigido em `app/__init__.py`).

- **B7 — CSP nonces.** Atualmente CSP tem `'unsafe-inline'` em scripts
  pra suportar `<script>` inline nos templates. Trocar por `nonce`
  por request (`secrets.token_urlsafe(16)` em context processor,
  anexar em cada `<script>`). Esforco: 1-2d, mexe em todos os
  templates com inline JS.

## VNDA aposentado (24/06/2026)

A integracao com VNDA (e-commerce externo) foi **aposentada** porque a
operacao migrou 100% pro sistema proprio (PedidoOnline / loja propria).
Decisao do dono em 24/06/2026. NAO ressuscitar sem ordem explicita.

**O que esta dormente** (codigo morto, mantido pra nao quebrar imports):
- Cron `vnda-sync` e `vnda-card-sync` em `app/services/seru_cron.py` — NAO
  agendados (linhas marcadas "VNDA APOSENTADO").
- `_agregar_vendas_vnda_api` em `app/services/vendas_manuais.py` — retorna
  sempre `({}, 'VNDA aposentado em 06/2026')`. NAO bate na API.
- `agregar_itens_consolidado` em `app/services/vendas_itens.py` — Seru +
  site (PedidoOnline) apenas; chaves `qtd_vnda`/`faturamento_vnda` ficam
  zeradas por compat com chamadores.
- `vendas_vnda_loja` ja usava `loja_online_vendas` desde o cutover parcial
  (22/06/2026) — mantida.
- Link "Vincular produtos do site" tirado da sidebar (`base.html`). Rotas
  `/pdv/vnda/...` continuam existindo mas sem entrada no menu.

**Trava de regressao**: `tests/test_vendas_vnda.py` e
`tests/test_vnda_cesta.py` testam que VNDA NAO eh consultado (patch da API
com `AssertionError` — qualquer chamada explode o teste).

**Painel de producao**: a previsao de producao foi reescrita pra usar o
historico de `PedidoLoja` (loja->industria) como base — nao mais PDV/VNDA.
Ver `app/services/previsao_producao.py::balanco_industria`. Coluna
"Produzir" = `max(0, max(comprometido, previsto) - em_estoque)`.

**Camada B (limpeza, em andamento)**: feito em 30/06/2026 — `vnda_sync.py`
REMOVIDO e os modelos `VndaProdutoMap` / `VndaPedidoProcessado` / `VndaDebito`
APAGADOS (a baixa do site roda pelo motor unico via `PedidoOnline`). As
tabelas `vnda_*` ficam no Postgres pra preservar historico (`db.create_all`
nao recria nem dropa). AINDA dormentes (camada CRM, intocada): `vnda.py`
(client da API), `vnda_card.py`, `/admin/vnda/contatos`, modelo `PedidoSite`
(cache do card de cliente do Chatwoot). Migrations antigas tambem ficam.

**Mapas unificados no `VendaMapa` (30/06/2026)**: o trio paralelo de
mapeamentos virou UM modelo `VendaMapa` com `canal` em {'seru', 'lote'}.
`SeruProdutoMap` (canal seru) e `LojaProdutoMap` (canal lote) seguem como
modelos CONGELADOS — so servem de fonte do backfill idempotente no cutover
de startup (`_cutover_baixa_venda`). O marcador loja<->mapa da tela de
mapeamentos de lote agora e `VendaMapaUso` (substitui o papel do `LojaDebito`).
NUNCA escrever de novo nos mapas velhos; toda leitura/escrita de mapeamento
(Seru, lote, congelados, relink de receita) passa pelo `VendaMapa`. O SITE
nao usa mapa (FK do `PedidoOnlineItem`).

## Stack

Flask 3 + SQLAlchemy + Bootstrap 5 + Postgres em prod / SQLite local.
Padaria Opão: receitas, pedidos, entregas, PDV, estoque, RH, copilot
(motor unico — Sonnet 4.6 no Slack, Opus 4.8 no WhatsApp do dono).

### Modelos Anthropic em uso (atualizado 14/06/2026)

**Copilot (motor compartilhado em `copilot.py::interpretar`)**:
- **Slack** (`slack_bot.py`, sem override): cai no `MODELO_DEFAULT =
  'claude-sonnet-4-6'`. 12 atendentes usam — Sonnet eh mais barato e o
  ganho de qualidade do Opus aqui nao compensou (decisao do dono
  14/06/2026 apos teste curto com Opus default).
- **WhatsApp do dono** (`zapi_bot.py`, override `modelo=
  MODELO_WHATSAPP_DEFAULT='claude-opus-4-8'`): Opus 4.8. Premium pro
  uso pessoal do dono, baixo volume, read-only.

**Outros canais**:
- **Bot de atendimento (Padeiro, Chatwoot)**: `claude-opus-4-8`
  (`chatbot.py:MODELO`). Decisao do dono: vale o custo extra pra o bot
  responder com confianca em vez de pingar perguntas.
- **OCR de NF/boleto (Contas a Pagar)**: `claude-opus-4-8` direto
  (`conta_pagar_ia.py:MODELO`). Sem cascata Sonnet->Opus.
- **Auditor** (`chatbot_auditor.py`): Sonnet 4.6.
- **Vigia chatbot** (`chatbot_vigia.py`): **Sonnet 4.6** (era Haiku 4.5;
  subido pelo dono em 25/06/2026 na padronizacao geral. ATENCAO: o vigia roda
  a CADA resposta do bot — `crm/routes.py` — entao e o de MAIOR volume; pesa
  no custo. A funcao `_chamar_modelo` foi renomeada de `_chamar_haiku` porque
  nao usa mais Haiku).
- **OCR de cupom** (`ocr_nota.py`): **Opus 4.8** (era Sonnet 4.6; subido pelo
  dono em 25/06/2026). Modelo inline, sem constante.
- **Follow-up pos-handoff** (`chatbot.py:FOLLOWUP_MODELO`): **Sonnet 4.6**
  (era Haiku 4.5; 25/06/2026).
- **Descricoes SEO** (`seo_descricoes.py:MODELO`): Sonnet 4.6 (era Haiku;
  25/06/2026).

**Padronizacao de modelos (dono, 25/06/2026)**: "tudo Sonnet 4.6, exceto bot
Chatwoot + WhatsApp do dono + OCR Contas a Pagar + OCR cupom = Opus 4.8".

**Instrumentacao de custo (25/06/2026)**: TODA chamada de IA agora registra
tokens + custo em USD na tabela `UsoIA`, rotulada por funcao, via
`app/services/uso_ia.py::registrar` (sessao isolada + best-effort, nunca
contamina transacao de negocio nem quebra o fluxo). Relatorio owner-only em
`GET /admin/uso-ia?dias=N` (por funcao, % do total, projecao mensal). Antes
disso o gasto por funcao era irrecuperavel (nada registrava). Precos em
`uso_ia._PRECOS` — atualizar quando a Anthropic mudar tabela.

**Regra "preferir RESPONDER a PERGUNTAR"** no system prompt (vale pra
Sonnet e Opus, mas rende mais no Opus): inferir/escolher com o contexto
em vez de pingar pergunta atras de pergunta. Excecao: WRITES de
dinheiro/estoque ainda exigem confirmacao. Ver
`copilot.py::_build_system_prompt` e `chatbot_prompt.py` (secao
"PREFIRA RESPONDER A PERGUNTAR").

## Schema migrations

**Alembic adotado em 21/05/2026** (Flask-Migrate). Coexiste com os helpers legados
`_migrate_postgres()` e `_migrate_sqlite()` em `app/__init__.py` por compatibilidade.

### Procedimento para mudança de schema (REAL — Alembic NAO roda em prod)

**ATENCAO**: o `Procfile` e `railway.json` rodam apenas `gunicorn run:app ...` —
NAO ha `release: flask db upgrade`. Migrations Alembic em prod estao dormentes.
Mudancas de schema em prod hoje sao aplicadas pelos helpers legados
`_migrate_postgres()`/`_migrate_sqlite()` em `app/migrations_legacy.py`, que
rodam no startup de cada worker gunicorn (idempotentes).

**Procedimento canonico (2 commits)**:

1. Adiciona `ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...` em
   `app/migrations_legacy.py::_migrate_postgres()` (e `_migrate_sqlite()` se
   necessario). Padrao: ver bloco da linha 344 (driver_id) — sondar
   `information_schema.columns`, condicional no nome.
2. Commit + push. Aguardar Railway deploy (~2-3min). Confirmar deploy aplicou
   antes de prosseguir (peca ao usuario; sem acesso ao Railway logs).
3. Edita o modelo em `app/models/<arquivo>.py` (adiciona coluna, relationship).
4. Commit + push. Modelo agora bate com schema real.

**Por que dois commits**: se o modelo for pushado antes do ALTER ter aplicado,
qualquer `SELECT` no modelo quebra com "column does not exist" e a UI fica em
500 (ja aconteceu em 2026-05-22 — ver "Versao canonica" acima).

### Alembic (local apenas, por enquanto)

Continua util pra:
- Versionar migrations no `migrations/versions/` (documenta historico).
- Testar local: `FLASK_APP=run.py flask db migrate -m "descricao"` +
  `flask db upgrade`.
- No futuro, se configurarmos `release: flask db upgrade` no Railway, todas as
  migrations versionadas viram aplicadas automaticamente.

Procedimento aplicado UMA VEZ na adocao (ja feito):
```bash
railway run flask db stamp head
```
Marca o banco como ja em baseline. **Nao rode novamente.**

### Configurar Alembic em prod (pendente)

Adicionar ao `railway.json` um `releaseCommand: "flask db upgrade"`. Risco:
se a primeira migration falhar, deploy nao sobe. Validar localmente com
banco snapshot de prod antes. Esforco: ~45min + validacao.

## Backup automatico do banco (2026-05-22)

Backup diario do Postgres pro Dropbox. Implementado em 22/05/2026.

- **Job**: APScheduler em `app/services/seru_cron.py::_run_backup_diario`,
  cron 04:00 BRT, advisory lock `LOCK_KEY_BACKUP = 7731`. Desligavel via
  env var `BACKUP_AUTO=0`.
- **Servico**: `app/services/backup.py::executar_backup()`. Roda
  `pg_dump --format=custom --no-owner --no-acl` via subprocess (senha
  via env `PGPASSWORD`, nunca em argv), comprime com gzip, sobe pro
  Dropbox.
- **Upload**: `app/services/dropbox_storage.py::upload_arquivo(bytes, path)`.
  Generic, com fallback chunked >140MB (limite Dropbox API simples).
- **Destino**: pasta `/backups-postgres/` no Dropbox da app
  `Receitas-Entregas`. Nome: `padaria_YYYY-MM-DD_HHMM.dump.gz`.
- **Rota manual**: `POST /admin/backup/run` (owner-only). Botao no
  `/admin/debug-schema` (card "Backup do Postgres → Dropbox").
- **Restore**: `pg_restore -d test_restore --no-owner --no-acl
  padaria_*.dump.gz`. **Importante**: dump custom format precisa de
  pg_restore (nao psql).

**Versao do pg_dump**: o Dockerfile instala `postgresql-client-18`
do repo pgdg porque o server Railway eh PG 18 e pg_dump precisa ser
>= versao do server. Quando server upgradear, atualizar Dockerfile.

**Drill de restore** (2026-06-09): `GET /admin/backup/drill?iniciar=1`
(owner) baixa o dump mais recente do Dropbox e valida o TOC com
`pg_restore --list` (~1 min). `?iniciar=full` restaura num banco
temporario `drill_restore_tmp`, conta linhas de tabelas-chave e dropa
(roda em thread; recarregar a rota mostra o status). Servico:
`backup.py::iniciar_drill/_executar_drill`. Fazer 1x por trimestre.

**Retencao de dados** (2026-06-09, LGPD + custo): limpeza automatica
roda no cron diario APOS o backup dar OK (nunca apaga o que nao esta
no dump do dia). Servico `app/services/retencao.py`. Prazos via env:
`RETENCAO_LOGS_DIAS=365` (VigiaVeredito),
`RETENCAO_CONVERSAS_DIAS=180` (ChatbotConversa inativa),
`RETENCAO_EVENTOS_DIAS=7` (idempotencia Slack/Zapi),
`RETENCAO_BACKUPS_DIAS=90` (dumps Dropbox — decisao do dono;
o dump MAIS RECENTE de cada pasta nunca eh apagado).
Desligar: `RETENCAO_AUTO=0`. Inspecao manual: `GET /admin/retencao`
(dry-run) / `?executar=1` (apaga). NUNCA adicionar tabela de negocio
(pedido/venda/estoque/RH) nos alvos — retencao eh so log/contexto/
idempotencia. **NFLog fica PRA SEMPRE** (decisao do dono 2026-06-10:
tudo que toca NF eh preservado; foi retirado dos alvos — ha teste
travando isso, `test_nflog_nunca_e_apagado`).

**Monitoramento de erros (Sentry)**: ja instalado e integrado
(`app/__init__.py::_init_sentry`, opt-in). Ativar = setar `SENTRY_DSN`
no Railway. Diagnostico: `GET /admin/debug-sentry` (owner) mostra
status; `?testar=1` manda evento de teste.

## Convenções de codigo

- **Lojas operacionais**: SEMPRE use `_lojas_operacionais()` em `pedidos/routes.py` —
  filtra "Industria" (que existe como Loja só pra RH). RH usa `Loja.query.filter_by(ativa=True)`
  normal.
- **Forms aninhados**: HTML não permite. Em `ficha.html` (receitas), os botões
  Duplicar/Excluir/Atribuir ficam como `<form>` **fora** do form principal de salvar.
- **Constantes de domínio**: use `app/constants.py` para listas compartilhadas
  entre services — `VENDA_TIPOS_LOJA` (Seru + VNDA, baixam de `EstoqueLoja`),
  `VENDA_TIPOS_PRODUCAO` (B2B, baixa de `EstoqueProducao`),
  `STATUS_PEDIDO_FINALIZADOS` (`entregue`/`recebido`/`cancelado` — os dois
  primeiros coexistem por histórico, sempre filtre os 3 juntos),
  `STATUS_PEDIDO_LABEL` (labels amigáveis pra UI/copilot). NUNCA duplique
  essas listas em arquivos individuais — quando divergem, geram bugs sutis
  (já aconteceu com `previsao_demanda` e `vendas_itens` ignorando VNDA, e
  com `consultar_pedido` esquecendo de filtrar `recebido`).
- **Fuzzy de loja por nome**: use `resolver_loja_por_nome()` em `app/utils.py`,
  não reimplemente o padrão `func.lower() / ilike` em cada service.
- **MP em pedido de loja é OPT-IN (07/07/2026)**: `MateriaPrima.
  sugerir_pedido_loja` (checkbox "sugerir pedido loja" no Banco de MPs) virou
  TRAVA do pedido loja→indústria — decisão do dono ("tenho itens que as lojas
  estão pedindo que não deveriam poder"). Camadas: typeahead
  (`pedidos/routes.py::buscar_itens` + `_mps_pediveis`), validação server-side
  do POST novo/editar (`_mps_nao_pediveis`), resolver do copilot
  (`_resolver_item_pedido`) e executores `executar_criar_pedido`/
  `executar_editar_pedido` (defesa em profundidade — preview re-enviado não
  fura). GRANDFATHER no editar: MP que JÁ está no pedido segue válida (web e
  copilot via `mp_ids_extras`; o GET do editar une as MPs do pedido à lista
  do select — sem isso o REPLACE derrubaria o item). Receitas e produtos
  seguem livres; `receber_mp`/`ajuste_estoque` continuam vendo TODAS as MPs
  (`_resolver_mp` intocado). Testes: `tests/test_mp_pedivel.py`.
- **Componente de cesta/sub-receita: FK manda, nome e so fallback (03/07/2026)**:
  `ProdutoItem.item_nome` e `ReceitaIngrediente.ingrediente_nome` podem ficar
  com grafia ANTIGA apos rename — todo lookup (custo, agregacao) deve usar
  `ProdutoItem.nome_resolvido` / resolver `sub_receita_id` primeiro. Caso real:
  receita do iogurte renomeada → cestas 200/600ml custavam so a embalagem e a
  margem inflava; pior, o nome velho no input da tela da cesta fazia o Salvar
  re-resolver a FK errado e ORFANAR o vinculo (baixa de venda parava em
  silencio). Fixado em `custos.py` (calcular_custo_produto,
  calcular_custos_produtos, _calcular_receita com id2nome),
  `produtos/routes.py::detalhe` (input mostra nome_resolvido) e rename de
  receita/produto sincroniza os nomes-fallback gravados. Testes:
  `tests/test_cesta_custo_fk.py`. NUNCA voltar a buscar custo por item_nome.
- **Timezone (CRÍTICO)**: SEMPRE use `app.utils.hoje()` (retorna `date` em BRT)
  e `app.utils.agora()` (retorna `datetime` em BRT naive). NUNCA use
  `date.today()` ou `datetime.now()` direto — no Postgres-Railway eles
  retornam UTC, e a partir das 21h BRT viram D+1 (causa bugs do copilot
  achando que é "amanhã" às 22h, lembretes de hoje sumindo, etc.).

## Estoque pendente (congelados + loja)

Tanto `EstoqueProducao` quanto `EstoqueLoja` tem coluna `nome_pendente`. Quando o
balanco/entrada-em-lote acha um item sem cadastro, cria linha com `nome_pendente` setado
+ `receita_id`/`produto_id`/`materia_prima_id` todos NULL. Admin vincula depois em
`/pedidos/congelados` ou `/pedidos/estoque-loja` (cards amarelos no topo). `_carregar_catalogo`
nos services inclui orfaos pra match — reaplicar o balanco com o mesmo nome reusa a linha.

## Ciclo de sobras: devolucao loja→industria + Croissant Almond (02/07/2026)

Caso de negocio (dono): croissants tradicionais que sobram nas lojas voltam
pra industria pra virar Croissant Almond (recheio de amendoas); Nutella e
Nutella c/ Morango sao recheados NA loja a partir da sobra que fica la.

**Devolucao de duas pontas** (`app/services/devolucao.py`): baixa `EstoqueLoja`
(mov `devolucao_industria`; falta vira `devolucao_industria_sem_estoque`, nunca
negativa) E credita `EstoqueProducao` (mov `retorno_loja`) na MESMA transacao.
Token `dev-<hex>` amarra as pontas; `estornar_devolucao(token)` reverte as duas
(idempotente; industria ja consumida = estorno parcial com aviso). Entradas:
tela `/pedidos/estoque-loja` (tipo "Devolver a industria" + card de devolucoes
recentes com estorno admin) e tool copilot `devolver_industria` (write,
admin+gerente, preview mostra o destino).

**Receita de retorno** (`Receita.retorno_receita_id`, self-FK; select na ficha,
admin): o credito da devolucao vai pra ELA (ex: Croissant Tradicional →
"Croissant Tradicional — Retorno"), nao pra receita original — a industria
mantem 1 linha por receita (`uq_estoque_producao_receita`) e o retornado
(assado, de vespera) NAO pode se misturar com o congelado cru que atende
pedidos. NULL = credita a propria. ALTER ja em `_migrate_postgres`/`_migrate_sqlite`.

**Politica "so de sobras" (decisao do dono 02/07/2026)**: receita cuja ficha
consome uma receita de RETORNO (ex: Almond consome 1:1 o "— Retorno") tem a
sugestao de producao CAPADA ao que o estoque devolvido cobre —
`previsao_producao._caps_por_retorno`, aplicado no balanco (`produzir` +
campo `limitado_por_retorno`) e no cronograma (`_explodir_bom` corta dos
ultimos dias + linha do retorno nunca sugere producao, `retorno: True`).
Retorno NAO e produzivel: nunca cascateia pra massa/MP. Limitacao conhecida:
dois pais consumindo o MESMO retorno nao rateiam (hoje so o Almond consome).
NAO "consertar" o cap pra puxar massa fresca sem perguntar ao dono.

**Nutella na loja**: os produtos "Croissant de nutella" (106) e "Croissant
Nutella com morango" (107) sao compostos de 1x **"Croissant Tradicional -
Retorno"** (conferido em prod 03/07/2026) — a venda baixa o RETORNO da loja
pelo motor unico. Croissant Tradicional marcado `reaproveitavel`.

**CONVERSAO da sobra no estoque da loja (decisao do dono 03/07/2026)**:
registrar sobra reaproveitavel de receita COM `retorno_receita_id` converte
o estoque DA LOJA na hora: baixa o fresco + credita a receita de retorno na
mesma loja (`desperdicio_core.converter_sobra_para_retorno`; movs
`sobra_retorno`/`sobra_retorno_entrada`/`sobra_retorno_sem_estoque`, todos
com `desperdicio_id` — a exclusao do desperdicio desfaz o par, revertendo a
entrada limitada ao saldo com aviso). Motivos: o fresco volta a refletir so
o vendavel (sugestao de pedido), a venda de Nutella baixa do retorno (que
antes ficava 0 pra sempre) e a retirada COLETA o retorno
(`executar_criar_retirada_sobras` troca o item pra receita de retorno; o
recebimento na industria credita o retorno = a propria). Reaproveitavel SEM
retorno configurado mantem o comportamento antigo (registro sem movimento).
Testes: `tests/test_sobra_conversao_retorno.py`.

**Regra da VESPERA do insumo (decisao do dono 10/07/2026)**: consumo de
insumo que cai DENTRO do lead dele (ex: croissants de HOJE consumindo massa
de lead 1d — a vespera ja passou) so pode ser coberto por ESTOQUE pronto.
O cronograma NAO agenda essa producao "o quanto antes" (bola de massa feita
hoje nao vira croissant hoje — caso real 10/07: 300 croissants HOJE puxavam
6 bolas HOJE, inuteis). O que o estoque nao cobre vira aviso visivel
`insumo_sem_vespera` na linha do insumo do /telaindustriateste (tag
"⚠ sem vespera" + box no expandir + celula ambar no dia do consumo) — o
dono acerta o estoque (massa na geladeira fora do sistema) ou revisa a
producao do pai. Implementacao: `previsao_producao._explodir_bom` (split
dentro_lead/gross; o estoque cobre PRIMEIRO o consumo iminente). Consumo
com vespera dentro do grid segue agendando em `dia_consumo - lead` como
sempre. Testes: secao "Regra da vespera" em `tests/test_cronograma_ux.py`.
NAO voltar ao "produzir o quanto antes" pra consumo dentro do lead.

**Massa para folhar em BOLAS + fracao acumulada (decisao do dono 03/07/2026)**:
a massa e contada em BOLAS inteiras (1 bola = 1 batida = 3.580g; padeiro
lanca "2", nao gramas). O consumo por lote e fracionario (batida de 50
croissants = 1,26 bola; croissant padrao 90g de massa) — `round()` por lote
sumia ~meia bola/dia. Agora `producao.consumir_subreceitas_prontas` faz
floor(consumo + acumulado) e guarda a fracao em `ConsumoSubFracao` (tabela
nova, 1 linha por sub-receita — mesmo padrao do SeruDebito). O MRP ja
acumulava fracao entre dias (previsao_producao.py ~1553). Cadastro: ficha
do croissant consome 1.257 bolas/batida (campo aceita fracao). Historico:
o "+900" de 01/07 foi o padeiro lancando em outra escala — o expandir de
/pedidos/congelados (5 creditos + 5 debitos por item) foi criado nesse
debug. Testes: `tests/test_consumo_sub_fracao.py`.

**Custo do retorno = CUSTO CHEIO da origem (decisao do dono 02/07/2026)**:
receita destino de retorno com ficha VAZIA herda o custo (e o peso unitario)
da receita de origem em `custos.calcular_custos_receitas` — o Almond carrega
o custo do croissant devolvido, nao R$ 0. Ficha preenchida no retorno =
override explicito (nao herda). Mais de uma origem pro mesmo destino: vale o
custo MAIOR. Consequencia ACEITA: o mesmo custo aparece 2x no agregado (na
producao do tradicional e dentro do Almond) — o dono preferiu margem
comparavel entre produtos a custo marginal. NAO reverter pra custo zero sem
perguntar.

**Bug corrigido no caminho**: o form da tela de estoque manda tipos
`sobra`/`perda` que NAO estavam em `TIPOS_VALIDOS` da rota registrar — caiam
no fallback `venda` (perda virava "venda manual" no historico). Agora sao
tipos reais (labels em `estoque_diario.py`). Testes:
`tests/test_devolucao_industria.py`.

**Retirada de sobras por QR (02/07/2026, fluxo principal)**: o dono quis a
devolucao NASCENDO no lancamento de sobras. Esteira em 2 TEMPOS, espelho das
entregas:
- Modelos `RetiradaSobra`/`RetiradaSobraItem`/`RetiradaQRCode`
  (`app/models/pedidos.py`) — SEPARADOS de PedidoLoja de proposito: retirada
  nunca entra em previsao/comprometido/medias (teste de regressao trava).
  Status: aguardando_coleta → em_transporte → recebida | cancelada.
- Fluxo bot: `registrar_desperdicio(_lote)` devolve `retirada_sugerida`
  quando o item e reaproveitavel + tem receita de retorno → o copilot
  pergunta "quantos voltam pra virar almond?", PEDE A FOTO da sobra
  (OBRIGATORIA — decisao do dono; sem imagem a tool recusa) e chama
  `criar_retirada_sobras` (write, aprovacao; admin+gerente+funcionario).
  Foto sobe pro Dropbox ANTES do registro. O slack_bot embute imagens nos
  params (mesmo mecanismo do anexar_foto_pedido) e o resultado posta o QR
  de coleta inline (`qr_texto` customiza a legenda em slack_blocks).
- Handshake `/handshake/r/<token>` (QR TTL 48h — criado na vespera):
  COLETA na loja (PIN de driver ativo) → em_transporte + BAIXA EstoqueLoja;
  RECEBIMENTO na industria (PIN de driver tambem — decisao do dono) →
  recebida + CREDITA a receita de retorno. PRG + double-submit + audit
  (tipos curtos `r_coleta`/`r_receb` — coluna VARCHAR(10)). Movimentos com
  token `ret-<id>`, MESMA familia do fluxo manual.
- Service em 2 tempos: `devolucao.baixar_loja_retirada` /
  `creditar_industria_retirada`. A tela/tool `devolver_industria` (atomica)
  segue como atalho manual pra excecoes.
- **Conferencia do motorista na COLETA (03/07/2026, pedido do dono)**: a
  tela do QR de coleta tem a quantidade POR ITEM editavel (default =
  declarado; aceita diferente, ex: loja marcou 15, saem 12).
  `RetiradaSobraItem.quantidade_coletada` (ALTER em migrations_legacy,
  procedimento 2 commits). Baixa da loja usa o COLETADO; recebimento na
  industria parte dele (`quantidade_recebida` > `quantidade_coletada` >
  declarada). Divergencia → post no `SLACK_CANAL_PEDIDOS` (fallback
  `SLACK_CANAL_COPILOT`) explicando que os que ficaram continuam no estoque
  de retorno da loja — as vendas de Nutella baixam dali, NUNCA dar entrada
  manual (duplicaria: nutella vendida antes do aviso ja baixou o retorno).
  Testes: `tests/test_retirada_coleta_divergencia.py`.
- Testes: `tests/test_retirada_sobras.py`. PENDENTE (nao bloqueia): mostrar
  retiradas do dia no Painel de Entregas e lista web com cancelamento.

**Fixes do primeiro uso real (02/07/2026 a noite, Nebraska — testes em
`tests/test_slack_retirada_e_duplicata.py`)**:
- A `retirada_sugerida` que o executor devolve MORRIA no caminho: o
  resultado pos-confirmacao virava so "✓ N desperdicio(s)" e o modelo nunca
  ficava sabendo (o botao roda FORA do loop do Claude) — a pergunta "quantos
  voltam?" nao acontecia e a retirada nao nascia. Agora: (1) o resultado no
  Slack ganha secao ♻️ pedindo quantidade + foto; (2)
  `slack_bot._apendar_contexto_retirada` grava o contexto na SlackConversa
  (mesclado no ultimo turno assistant) pra o modelo saber chamar
  `criar_retirada_sobras` quando o usuario responder.
- **Pergunta NA HORA (03/07/2026, cobranca do dono)**: o combinado sempre
  foi o bot perguntar "quantos voltam?" NO MOMENTO em que a sobra e falada,
  nao depois do botao. O `interpretar` e um tiro so (o texto do modelo sai
  ANTES do enrich), entao a pergunta nasce no PREVIEW:
  `copilot._retirada_sugerida_preview` calcula a sugestao no enrich
  (single + lote → `params['retiradas_sugeridas']`),
  `slack_blocks._blocos_pergunta_retirada` mostra o ♻️ "Quantos vao
  voltar?" no proprio preview, e `slack_bot._pergunta_retirada_para_historico`
  grava a pergunta no turno assistant do historico — o modelo entende o
  "10" + foto que vier em seguida e chama `criar_retirada_sobras`.
- **MODO RESTRITO precisa ver a tool de retirada**: o canal de sobras roda
  com o bot de pedidos OFF (`SLACK_BOT_PEDIDOS_ATIVO=0` desde 28/06) e a
  whitelist `_TOOLS_DESPERDICIO` NAO tinha `criar_retirada_sobras` — o bot
  perguntava "quantos voltam?" e nao tinha como agir na resposta (usuario
  mandou foto + quantidade e o bot "nao entendia"). A tool entrou na
  whitelist + `_SYSTEM_DESPERDICIO` instrui o fluxo (retirada exige FOTO).
  Ao mexer no ciclo de sobras, conferir se a tool nova esta na whitelist
  do modo restrito.
- Lote de desperdicio DUPLICADO: o modelo re-enviou a lista inteira pra
  acrescentar 1 item (almond) e 4 itens duplicaram como perda. Defesas:
  aviso "⚠ Ja registrado HOJE nesta loja" no preview
  (`_enriquecer_registrar_desperdicio_lote` marca `ja_registrado_hoje` por
  item) + regra na tool/prompt (re-envio so com o item que faltou).
- CLAIM atomico no Confirmar (`processar_interacao_botao`): `executado_em`
  so era setado DEPOIS do executar — dois cliques quase simultaneos
  executavam a acao 2x. UPDATE condicional antes de executar; o perdedor ve
  "ja processada". Falha na execucao limpa o claim e marca `cancelado_em`.
- Vocabulario de motivo do preview do lote realinhado com o executor
  ('nao_vendeu' era silenciosamente virado 'vencido' no preview).
- **Exclusao de desperdicio com estorno EXATO (03/07/2026)**: cada
  `MovEstoqueLoja` de desperdicio agora carrega `desperdicio_id` (ALTER em
  `migrations_legacy`, deployado ANTES do modelo — procedimento de 2
  commits). `POST /pedidos/desperdicio/<id>/excluir` (admin) estorna pelos
  movimentos vinculados: baixa parcial devolve so o que saiu; cesta devolve
  nos componentes; reaproveitavel (nunca baixou) nao credita nada; registro
  ANTIGO sem vinculo e excluido SEM mexer em estoque, com aviso. NUNCA
  regredir pra "creditar desp.quantidade as cegas" — cria estoque fantasma
  nos 3 casos acima. Testes: `tests/test_desperdicio_excluir_estorno.py`.

## Cronograma — motor de previsao selecionavel (06/07/2026)

`/telaindustriateste` tem o seletor **"Prever por"** (pedido do dono: "+1
opcao de previsao baseada nas vendas"): **pedidos** (historico de pedidos
loja→industria — default/original), **vendas** (venda real das lojas +
merma estrutural — MESMA demanda unificada da Fase 0.1, so receita_id) ou
**maior** (max dos dois POR DIA). O firme conta SEMPRE, em qualquer motor.
Param `motor=` atravessa `balanco_industria`/`cronograma_producao`/
`editar_celula`/`aprovar_plano_do_dia`/`enviar_plano_do_dia`/
`decompor_previsao` e a API do assistente (`/api/claude/cronograma?motor=`).
Aprovar/enviar usa o motor DA TELA (mesma regra do equilibrar — senao a
ordem nao bate com o grid visto). Cache do balanco tem motor na chave.
Constante: `previsao_producao.MOTORES_PREVISAO_PRODUCAO`. Testes: secao
"motor de previsao" em `tests/test_cronograma.py`.

## Pré-baixa de MP na ordem enviada (07/07/2026)

Pedido do dono: ENVIAR a ordem ao padeiro dá uma PRÉ-BAIXA nas MPs; a
confirmação do padeiro dá a baixa de fato. Implementação:

- **Modelo `PreBaixaMP`** (`app/models/producao.py`): 1 linha por
  (plano, MP) com a quantidade reservada. Linha 0 = marcador de REGIME;
  plano sem NENHUMA linha = enviado antes da feature (não se pré-baixa
  retroativo). Tabela nova via `db.create_all` (sem ALTER).
- **Reconciliador idempotente** `producao.sincronizar_pre_baixa_mp(plano,
  user_id, criar=False)`: alvo = explosão da FALTA (alvo − produzido dos
  itens não dispensados) se `enviado_ao_padeiro`, vazio se rascunho.
  Aplica só o DELTA como `MovimentacaoEstoque` ('saida' "Pré-baixa
  produção dd/mm" / 'entrada' "Estorno pré-baixa produção dd/mm") +
  `estoque_atual`. `criar=True` só nos gestos explícitos de envio
  (`enviar_plano_do_dia` e `reagendar_para_hoje`). NUNCA mexer nas
  quantidades de `PreBaixaMP` por fora do reconciliador.
- **Explosão**: MESMO motor da baixa real e da calculadora de compras
  (`consolidar_lista_compras`, com o fix mp_un/mp_direto de 04/07) e MESMO
  rendimento do produzir (`rendimento_massa_crua`) — por isso a
  confirmação troca pré-baixa por baixa real EXATO (estoque líquido não
  muda no confirmar; era isso que o dono queria: reservar no envio).
  Sub-receitas prontas (congelado) ficam FORA — a pré-baixa é só de MP.
- **Caminhos ligados**: `enviar_plano_do_dia` (cria/ajusta delta; plano
  que ficou vazio libera), `produzir_item_plano` (libera a fração
  confirmada — a baixa real de sempre acontece igual),
  `dispensar_item/itens` (libera a falta dispensada), `reverter_dispensa`
  (re-reserva), `reagendar_para_hoje` (libera na origem, reserva no plano
  de hoje), `excluir_plano_do_dia` (`estornar_pre_baixa_plano` devolve
  tudo e apaga as linhas). Aprovar (rascunho) NÃO reserva.
- Estorno de pré-baixa tem `preco_unitario=None` → vale R$ 0 nos
  relatórios de compra (mesmo padrão dos estornos de pedido).
- Testes: `tests/test_pre_baixa_mp.py` (10 casos, incl. idempotência do
  re-envio, delta de edição de grid e ordem antiga fora do regime).

## Cronograma — ordem ENVIADA nunca muda por caminho implicito (04/07/2026)

Garantia do dono: depois do "enviar a producao", o que o padeiro ve so muda
pelo "🔄 atualizar producao" explicito daquele dia. Mapa dos caminhos:
- `limpar_todos_overrides` / `editar_celula` / `celula_reset` mexem SO em
  `CronogramaOverride` (rascunho do grid) — nunca em `PlanejamentoProducao`.
- `aprovar_plano_do_dia` num dia ja ENVIADO levanta `PlanoJaEnviadoError`
  (recusa sem tocar o plano) — antes reconstruia os itens da ordem em
  execucao se um POST/aba desatualizada chamasse aprovar (furo fechado).
  Re-aprovar RASCUNHO continua permitido.
- `enviar_plano_do_dia` e o UNICO que reconstroi ordem enviada (gesto
  explicito, re-pressavel). Testes: `tests/test_cronograma_ordem_enviada.py`.

## Cronograma — ordem enviada de volta na tela + cadeado por dia (08/07/2026)

Dois pedidos do dono, ambos no grid do /telaindustriateste:

**1. Ordem ENVIADA visivel quando o grid diverge.** Antes, editar o grid num
dia ja enviado ficava so como rascunho (`CronogramaOverride`) e a diferenca
pro que o padeiro esta vendo era INVISIVEL ate alguem lembrar do "🔄 atualizar
producao". Agora o `index` compara, por celula, `esperado = max(qtd_grid +
qtd_extra, produzido)` com o `qtd_alvo` da ordem (a MESMA conta do
`_sync_itens_do_cronograma` — por isso o re-envio zera exato a divergencia). A
celula que difere mostra "📤 N" (o numero que o padeiro ve) e o cabecalho do
dia ganha "⚠ difere do enviado" + aviso no KPI. So dias VISIVEIS no grid
(`dias_grid`) entram na conta — plano de fora do horizonte (ordem de ontem)
nao tem coluna e marcaria diferenca falsa. Item DISPENSADO fica fora dos dois
lados: a dispensa e decisao explicita e o sync mantem `dispensada_em`, entao
comparar geraria um "difere" que nenhum re-envio limpa.

**2. Cadeado por dia (🔒).** Modelo `CronogramaDiaFechado` (tabela nova via
`db.create_all`, sem ALTER). Dia fechado: `editar_celula` recusa
(`{'erro':'dia_fechado'}`, rota `/celula` devolve 422), `limpar_todos_overrides`
PRESERVA os overrides do dia (agora retorna `(apagados, preservados)` — TODOS
os chamadores atualizados), `resetar_receita` PULA as datas fechadas (retorna
`(apagados, preservados)`; o JS avisa quando preservou). Enviar/aprovar/excluir
ordem CONTINUAM permitidos em dia fechado — o cadeado protege o RASCUNHO, nao a
ordem ("fechei, agora envio"). Toggle `POST /dia/cadeado` (admin, CSRF via
`campos_dia`). ARMADILHA fechada: a mao-dupla do `/padeiro/plano/editar`
espelhava a ordem no `CronogramaOverride` sem checar o cadeado — agora pula o
espelho em dia fechado (a ordem pode mudar, o rascunho protegido nao).
`podar_dias_fechados_passados()` roda no GET (cadeado de dia que ja passou
sumiria do grid mas blindaria overrides mortos do "limpar"). Toggle tem
try/except IntegrityError (duplo-clique no unique de `data`). Testes:
`tests/test_cronograma_fechado_e_difere.py`.

## Cronograma — UX de orientacao + MP do dia (10/07/2026)

Pedido do dono ("me sinto perdido nela"), tudo em /telaindustriateste:

- **Painel "📋 Proximos passos"**: lista `acoes` computada na rota `index`
  (enviar hoje sem ordem / rascunho esquecido / grid difere / producao
  vencida / entregas em risco / edicoes stale), cada item com o gesto ao
  lado. Some quando nada e acionavel. So estados ACIONAVEIS viram item.
- **Trilha de dias** (chips clicaveis): estado por dia (enviado/rascunho/
  sem ordem/difere/🔒/🔥 pico) + total un/fornadas; clique rola o grid ate
  a coluna e pisca (th tem `data-data`).
- **Explicador** reescrito: fluxo em 5 passos + legenda visual + glossario.
- **Ordenacao do grid** (JS client-side, localStorage `crono.ordem`):
  categoria/maior producao/risco/nome. Fora de "categoria" os cat-rows
  ficam ocultos; a ponte com os filtros e o evento `crono:refiltrar` +
  `tbody.dataset.ordem` (sem isso o walk de categorias ressuscitava
  cabecalho orfao — pego em revisao).
- **"🧾 Materia-prima do dia"** (menu ⋯ de cada dia → modal): rota
  `GET /telaindustriateste/mp-dia` → `producao.mp_necessaria_do_dia`
  (read-only). Explode o GRID do dia (com overrides) na MESMA conta da
  pre-baixa/baixa real (`consolidar_lista_compras` + multiplicador
  fracionario qtd/`rendimento_massa_crua`) e compara com estoque de MP.
  Dia ja ENVIADO: credita de volta a reserva de pre-baixa DESTE dia (sem
  isso o insumo ja reservado aparecia como falta — falso alarme).
  Ingrediente de ficha sem MP cadastrada vira aviso `sem_cadastro`
  (nao some em silencio). Link pra acuracia da previsao no topo
  (`producao.previsao_acuracia`).

Testes: `tests/test_cronograma_ux.py`.

## Tela do padeiro — ordem persiste apos a meia-noite (03/07/2026)

O padeiro trabalha de MADRUGADA: a ordem do dia D e executada na madrugada de
D+1. Na virada da meia-noite `hoje()` rola e a ordem sumia da tela (buscava so
o plano de `data == hoje`). A visao "hoje" do `/padeiro` agora mostra tambem a
**ordem de ONTEM em aberto** (falta > 0, itens nao dispensados) num card ambar
acima da producao do dia — `padeiro/routes.py::_plano_em_aberto` + macro
`plano_card` no template (o `verBase(mbId, data)` escala a massa-base pela
data do plano da secao, nao pelo dia da tela). O card some quando o padeiro
produz tudo OU o admin dispensa na auditoria. So D-1 (nao lista vencidos
antigos — esses sao papel da auditoria; lista-los duplicaria producao, pois o
cronograma re-sugere demanda descoberta). Testes:
`tests/test_padeiro_plano_ontem.py`.

## Impressao de pedidos de entrega (2026-06-12)

**A impressao oficial e PDF gerado no servidor** (`app/services/pdf.py::
gerar_pedidos_pdf`, rota `GET /entregas/imprimir.pdf`, fpdf2 ja no
requirements). A pagina HTML `/entregas/imprimir` e SO preview de tela —
**NUNCA reintroduzir `window.print()` nela**: o Safari re-renderiza/
re-busca o documento ao montar a impressao e quebrou 3 vezes seguidas em
11/06/2026 (paginas duplicadas com `min-height` 270mm > area util de
269mm; paginas em branco com flex na `.folha`; conteudo apagado ao apertar
imprimir). Contagem de paginas e garantida no servidor:
`len(pedidos) x len(vias)` (teste trava).

**Fluxo**: JS POSTa `pedidos_json` (estado em memoria das DUAS abas) →
servidor persiste em `ImpressaoLote` (tabela efemera, lotes >2 dias
varridos a cada POST) → 303 pro GET `?lote=<token>` (PRG) → botao verde
abre o PDF. Debug de lote: `GET /entregas/imprimir/debug/<token>`
(admin/owner).

**Kit/box do VNDA (ex: "Box Mimo") — decisao do dono (2026-06-12)**: a
API VNDA manda `price`/`subtotal` = 0 nos itens desses produtos; o
dinheiro so existe no `total` do pedido. Na impressao, a coluna Valor e
OMITIDA quando todos os itens sao 0 e total > 0 (`pdf.py::
itens_sem_valor`) — NUNCA ratear o total entre itens (inventar valor
financeiro). Consequencia conhecida e ACEITA pelo dono: o faturamento
VNDA (`vnda_sync.py:94-95`) PULA pedidos com itens todos zerados — o
relatorio subconta as vendas de kit/box. NAO "corrigir" sem perguntar
(a alternativa, usar `order['total']`, incluiria frete e mudaria a
semantica de comparacao com o Seru).

## Integracao Seru (PDV)

**Documentação**: https://integration.plataformaseru.com.br/v1/docs.
Credenciais: `SERU_CLIENT_ID` + `SERU_CLIENT_SECRET` no env Railway.

**Service base**: `app/services/seru.py` (OAuth2 client_credentials, token cache,
paginacao por dia em paralelo). `data_local()` converte UTC → BRT.

### Fase 1 — Relatorio
- Rota `/pdv/itens-vendidos` + API `/pdv/api/itens-vendidos`
- Service `app/services/vendas_itens.py` agrega por produto com match fuzzy local
- Tool copilot `consultar_vendas_itens` (read, admin+gerente)
- **Separado por loja + XLSX (01/07/2026)**: a tela mostra uma secao recolhivel
  por loja + "Consolidado"; `agregar_itens_por_loja` + `gerar_xlsx_itens_por_loja`
  (uma aba por loja + Consolidado). Rotas `/pdv/api/itens-vendidos-por-loja` e
  `/pdv/itens-vendidos.xlsx`. `montar_linhas` centraliza a forma da linha.

### Persistencia de vendas — `VendaSeruDiaria` (Passo 1, 01/07/2026)
O relatorio re-consultava a API a CADA request; com ~600 pedidos/dia isso
estourava em ranges largos (o "erro de rede" do Safari na tela). Agora
`app/services/vendas_diarias.py` grava um snapshot por (data, loja_seru,
seru_nome, qtd, faturamento `Numeric`) em `VendaSeruDiaria`:
- `capturar_periodo(di, df)`: bate a API 1x e regrava o intervalo (idempotente —
  apaga+insere; dia todo cancelado zera). `data` = createdAt BRT.
- `agregar_por_loja_do_banco(di, df)`: le do BANCO na mesma forma do relatorio.
- As rotas de itens-vendidos/XLSX leem do banco por padrao, capturando so os
  dias faltantes + SEMPRE hoje, com fallback gracioso se a API cair. `?ao_vivo=1`
  forca a API (sem persistir) — util pra comparar.
- Cron (`seru_cron`): apos cada sync captura hoje+ontem (snapshot quente,
  resiste a API fora na hora do relatorio). Best-effort, nunca derruba o sync.
- Backfill owner em background: `POST /pdv/vendas-diarias/backfill` (botao
  "Aquecer historico") pre-carrega o passado semana a semana.
- NAO substitui `MovEstoqueLoja` (baixa de estoque) — e a fonte do RELATORIO/
  faturamento. A previsao Maneira 2 continua lendo `MovEstoqueLoja`.

**Passo 2 (01/07/2026) — TODOS os relatorios leem do banco**:
- `VendaSeruDiaLoja` (companheira): totais por (data, loja) — pedidos DISTINTOS
  (somar n_pedidos por PRODUTO inflaria: 1 pedido/3 itens = 3x) + DOIS
  faturamentos: `faturamento` = soma dos subtotais dos itens (base do relatorio,
  subconta kit/box) e `faturamento_pedidos` = soma do TOTAL do pedido (inclui
  kit/box, base do faturamento do bot). Os dois de proposito — nao unificar.
- `agregar_flat` (relatorio consolidado), `agregar_por_loja_do_banco`,
  `faturamento_por_loja` + `garantir_capturado` (captura dias faltantes + hoje,
  best-effort). Consumidores repointados pro banco: reconciliacao
  (`pdv_saude.reconciliar`), copilot `consultar_vendas_itens` +
  `agregar_itens_consolidado`, `/api/bot/faturamento` (dinheiro — usa
  `faturamento_pedidos`, sem regressao de kit/box), `/pdv/api/itens-vendidos`.
**Passo 3 (01/07/2026) — "Vendas PDV" (`/pdv/api/vendas`) repointada pro banco**:
- Tabela nova `VendaSeruDiaBreakdown` (data, loja_seru, `dimensao` in
  {'pagamento','canal','cancelados'}, chave, valor) guarda os eixos que a tela
  precisa e que `VendaSeruDiaLoja` nao tem. Pra 'cancelados', `valor` = CONTAGEM.
  Nova tabela → criada por `db.create_all` (nao precisou de ALTER; NAO alterei
  `VendaSeruDiaLoja` de proposito, pra evitar o trap de add-column caso o Passo 2
  ja tivesse subido em prod).
- `capturar_periodo` agora grava tambem os breakdowns (pagamento usa
  value|total|amount; canal usa o TOTAL do pedido; cancelados = contagem por
  loja) — MESMA logica do endpoint ao vivo (`_str_chave` espelha o `_s` da rota).
- `vendas_diarias.vendas_pdv_do_banco(di, df)`: le do banco e devolve os totais
  globais + `por_loja_detalhe[loja] = {total, n_pedidos, cancelados,
  por_pagamento, por_canal}` pra o filtro por loja da tela funcionar SEM os
  pedidos crus.
- `_api_vendas_impl` le do banco por padrao (`fonte='banco'`, `pedidos=null`);
  `?ao_vivo=1` volta a bater na API e traz o detalhe pedido-a-pedido (util pra
  ranges curtos). O front (`pdv/index.html`) ramifica: modo banco usa os
  agregados + `por_loja_detalhe`; modo ao vivo reagrega dos pedidos crus. O
  detalhe pedido-a-pedido NAO fica no snapshot — a tela mostra um aviso com botao
  "Ver detalhe ao vivo".
- **Faturamento por loja/global**: usa `faturamento_pedidos` (TOTAL do pedido,
  inclui kit/box), igual ao caminho ao vivo. `total_pedidos` no JSON inclui
  cancelados (o front faz `total - cancelados`), pra casar a semantica.
- Backfill/cron: como `capturar_periodo` agora grava os 3 snapshots juntos, o
  "Aquecer historico" e o cron ja populam os breakdowns sem mudanca.

### Saude da API Seru — `/pdv/debug-seru` (owner)
Testa auth + 1 request real e mostra o erro EXATO da API (nunca vaza segredo,
so presenca/tamanho). Usar quando a busca/sync falhar pra saber se e a API ou o
navegador/webview. Botao "Debug Seru" no topo de `/pdv/itens-vendidos`.

### Fase 2 — Auto-baixa estoque
Mapeamentos persistentes em 3 tabelas:
- **`SeruProdutoMap`**: nome Seru → receita/produto + estado (mapeado/ignorado/pendente)
  + `fator_quantidade` (Float, default 1.0) pra produtos compostos
  (ex: "NOZES COM MANTEIGA" = 2 fatias / 10 por pao = fator 0.2)
  - **REGRA DE NEGOCIO confirmada pelo dono (2026-06-10): os CAFES/EXPRESSOS/
    CAPPUCCINOS/CHA mapeados pro "Cookie Calebaut" com fator 0.2 estao
    CERTOS** — a loja corta o cookie em 5 e serve 1 pedaco com cada cafe.
    NAO eh bug, NAO "corrigir". (Eu, Claude, quase induzi o dono a quebrar
    isso achando que era mapeamento errado em massa — intuicao de engenheiro
    NAO substitui regra de negocio local. Confirmar com o dono antes de
    mexer em mapeamento que parecer estranho.)
- **`SeruLojaMap`**: company.name Seru → Loja. Auto-fuzzy NÃO basta — exige `confirmado_em`
  preenchido (admin clica OK ou Vincular em `/pdv/mapeamentos`) pra processar baixas.
- **`SeruPedidoProcessado`**: idempotencia por seru_pedido_id; cancelados depois geram
  estorno automatico (mov tipo `venda_seru_estorno`).
- **`SeruDebito`**: acumulador por (loja, mapping) pra fracoes — quando `fator < 1`,
  acumula ate `>= 1.0` e baixa inteiros. Mantem `EstoqueLoja.quantidade` sempre inteiro.

**Processador**: `app/services/seru_sync.py::processar_pedidos(data_ini, data_fim, user)`.
Idempotente. Salvaguardas:
- Loja nao-confirmada → pedido NAO marca como processado, retenta na proxima sync.
- Produto pendente/ignorado → pula sem alarme.
- Estoque negativo → registra `venda_seru_sem_estoque` em vez de zerar.

**Cron**: `app/services/seru_cron.py` inicia APScheduler 15min no startup do app.
`pg_try_advisory_lock(7723)` garante exec unica entre workers gunicorn.
Desligar: env `SERU_AUTO_SYNC=0` (default `1`).

**UI mapeamentos**:
- `/pdv/itens-vendidos`: cada linha tem botao "Editar" que abre modal inline com
  Vincular/Ignorar/Desfazer + campo fator (helper: "X fatias de Y" → calcula).
- `/pdv/mapeamentos`: tabela completa de produtos + tabela de lojas. Form POST normal
  (scroll preservado via `sessionStorage`).

## Frete do site — geocode com sanidade de CEP (05/07/2026)

Incidente: a BrasilAPI PAROU de devolver coordenadas de alguns CEPs (mudanca
do lado dela, zero deploy nosso) e o fallback por texto do `frete.py`
aceitava rua HOMONIMA de outra cidade/bairro em silencio — "Rua Nova York"
(Brooklin, 500m da padaria) caiu na homonima do Grajau (19,3km → R$95 no
checkout) e o CEP 01050-000 (Centro, 7,4km) caiu na "Rua Martins Fontes" de
ARUJA (44km → bloqueado como "fora da area"; caso D Lucas, venda quase
perdida). Fix: `_geocodificar_texto` pede `addressdetails` ao Nominatim e
DESCARTA candidato cujo postcode diverge do CEP do cliente (prefixo de 4
digitos = mesmo distrito; ve ate 3 candidatos). Nenhum candidato compativel
→ 'nao_encontrado' (checkout pede pra conferir o endereco) — NUNCA aceitar
coordenada divergente so pra dar um numero. Testes: 4 casos em
`tests/test_frete.py` (secao "Sanidade de CEP"). Sonda de diagnostico:
`GET /api/claude/frete-debug?q=<endereco|cep>` (mostra cada etapa da cadeia
com lat/lng/distancia — usar SEMPRE que suspeitar de frete errado).
No mesmo incidente: `_EPS_ULP` no ceil da sugestao de pedido
(`previsao_producao.py`) — media de recencia que da inteiro exato podia
sair 1 ulp acima e o ceil inflava +1 unidade/caixa (CI flakava no
test_lote_producao; media exibida 7,0 e sugestao 8).

**Vigia do SITE** (pedido do dono na sequencia — "scan diario pra ter
certeza que tudo funciona"): `app/services/site_vigia.py`, cron 2/2h em
`seru_cron` (lock 7745, kill-switch `SITE_VIGIA=0`). Canarios rodam as
MESMAS funcoes do checkout: frete com faixas esperadas (padaria <=1,5km;
Rua Nova York <=5km; 01050-000 dentro 3-13km; Campinas SEMPRE fora — pega
o defeito inverso), vitrine com item vendavel (`produtos_publicados` — NAO
bate na rota HTTP: o gate de host da loja da 404 fora do opao.online) e
agenda com data+janela de entrega. Alerta WhatsApp do dono na transicao
saudavel→doente, re-alerta 6h, aviso de normalizacao (mesmo padrao do vigia
de infra, estado em AppConfig). Sob demanda: `GET /admin/vigia-site`
(owner; `?alertar=1` roda o fluxo com WhatsApp). Testes:
`tests/test_site_vigia.py`. Ao mudar faixa de frete/area de entrega,
ATUALIZAR os canarios junto.

### Geocode agora usa GOOGLE primeiro + sensor de venda barrada (09/07/2026)

Contexto: a cadeia gratis (BrasilAPI+Nominatim) segue instavel — barrou vendas
reais (caso Alane, "Rua Guararapes" homonima Brooklin×Lapa, CEP sem coordenada;
caso Paulinha) e, PIOR, mandaria a Lalamove pro lugar errado (ela usa o MESMO
`frete.geocodificar`). O Google (que ja existia no sistema pra rotas de
entrega — `app/services/google_maps.py`, com `GeocodeCache`) e preciso a nivel
de porta. Decisao do dono: usar o Google como fonte PRIMARIA do frete.

**Por que o frete nasceu SEM Google (10/06)**: o `/api/frete` e PUBLICO
(anonimo, por clique) e o Google COBRA por chamada — manter em fonte gratis
blindava custo/abuso. O dono lembrava que "aconteceu algo" mas nao o que (git
nao registrou — auto-commit gera "auto: update X"). Por isso o Google entrou
COM tetos, nao solto.

**Ordem em `frete._geocodificar_impl`** (devolve `(geo, impreciso, fonte)`):
0. "lat,lng" colado; 1. **GOOGLE** (`_google_geocode`→`google_maps.geocode_preciso`);
2-6. cadeia gratis como FALLBACK (BrasilAPI→texto→simplificado→rua+cidade→CEP
centroide). `fonte` in {latlng, google, gratis, cep_centroide}.

**Blindagens do Google** (o motivo provavel do incidente = custo):
- Kill-switch `FRETE_GOOGLE=0`.
- Teto DIARIO `FRETE_GOOGLE_MAX_DIA` (default 500) — `_google_sob_teto` conta so
  chamadas REMOTAS (cache hit nao consome), via AppConfig `frete_google_dia`.
  Cap SOFT (read-modify-write entre workers vaza um pouco), mas custo fica ~teto*
  US$0,005/dia = ~US$2,50. Trava DURA de verdade = restringir a chave por IP no
  Google Cloud (pendente do dono).
- Cache permanente (paga 1x por endereco). `geocode_preciso` REJEITA
  `location_type=APPROXIMATE`/`partial_match` (centroide de cidade) — cacheia
  como `google_aprox` e cai na gratis, NUNCA cobra aproximado como preciso.
- `_google_geocode` e todo try/except: fora de app-context (thread do bot) ou
  sem chave → None → cadeia gratis. Sem regressao.

**Sensor de venda barrada** (`FreteSensor` em `models/entregas.py`,
`app/services/frete_sensor.py`): grava eventos que barram/erram venda —
`barrado` (nao localizou), `impreciso` (cotou pelo centroide do CEP),
`fora_area` (alem do raio), `resolvido_google` (checkout), `lalamove_falhou`.
Sessao ISOLADA (`Session(db.engine)` — nao contamina o checkout), best-effort,
dedup leve (10min), kill-switch `FRETE_SENSOR=0`. Painel owner
`/admin/frete-sensores` (link em Administracao) mostra contagens + uso/custo
Google do dia + lista com endereco/contato. Alem do alerta WhatsApp ja
existente (`loja_alerta.alertar_endereco_falho`, dedup + teto/hora). PII →
RETENCAO (`RETENCAO_FRETE_SENSOR_DIAS`=90, LGPD). Testes: secao Google em
`tests/test_frete.py`, `tests/test_frete_sensor.py`, `tests/test_loja_alerta.py`.

**Cobertura de notificacao (09/07/2026, "vou saber se der erro no CEP?")**: o
alerta ao dono migrou de `impreciso=bool` pra `motivo=str` (dict `_MSG_MOTIVO`
em `loja_alerta.py`: `nao_encontrado`/`impreciso`/`fora_area`/`lalamove` — chave
de dedup inclui o motivo). Duas lacunas fechadas: (1) **Lalamove** que nao acha
o destino manda WhatsApp na hora (`motivo='lalamove'`) alem do sensor —
motoboy nao sai, o dono precisa saber; (2) **fora da area** registra no painel
SEMPRE (`fora_area`), e pinga WhatsApp quando ficou PERTO da borda —
`distancia_km <= RAIO_MAX_KM + MARGEM_ALERTA_FORA_KM` (25+5=30km; "quase
comprou") — OU quando o km e INCERTO (`impreciso=True`, coordenada do centroide
do CEP: o endereco real pode estar DENTRO da area, entao alerta mesmo alem dos
30km — decisao do dono pos-revisao). Longe E preciso (outra cidade, fonte
google/gratis exata) = so painel, sem inundar o WhatsApp. O alerta `lalamove` e
ISENTO do teto/hora (pedido pago, caminho interno — nao pode ser suprimido por
ruido do endpoint publico). Ordem dos ramos importa: `fora_area` e checado
ANTES de `impreciso` (o dict do fora_area tambem carrega `impreciso=True` — sem
a ordem, dispararia os dois).

**COMPLEMENTO fora do geocode (11/07/2026, caso Mooca)**: o alerta pegou uma
venda barrada real — "Rua Joao Antonio de Oliveira, 544, Ape 502 Positano,
Mooca, CEP 03111-010" caiu em `nao_encontrado` embora o endereco fosse VALIDO e
dentro da area (~10km, R$50). Causa: a string de geocode incluia o COMPLEMENTO
("Ape 502 Positano"); nome de predio + "Ape 502" fazem o Google devolver
`partial_match` (rejeitado pelo `geocode_preciso`) e derrubam o Nominatim — a
cadeia inteira falha. O dono reproduziu SEM complemento e cotou na hora. Fix:
`loja_checkout._montar_endereco(form, incluir_complemento=False)` gera a string
PRA GEOCODE sem complemento (rua+numero+bairro+cidade+CEP); o snapshot de
entrega (`endereco_entrega`) e o cliente-side `enderecoMontado()` (checkout.js)
MANTEM o complemento pro motorista/registro. REGRA: complemento (apto/bloco/
nome de predio) NUNCA entra na consulta de geocode — so ajuda o motoboy, so
atrapalha o geocoder. Testes: `test_geocode_do_frete_nao_leva_complemento` em
`tests/test_loja_checkout_v2.py`.

**`consultar_frete` agora devolve `fonte` e `impreciso`**; `api_frete` e
`_frete_para` alertam+sensoreiam nos casos de risco. NUNCA remover o Google do
frete sem entender o custo (checar billing do Google Cloud) — e NUNCA deixar o
Google cobrar resultado APPROXIMATE como preciso.

## Estoque do site — DUAS camadas separadas (regra do dono, 07/07/2026)

Escrito na pedra a pedido do dono ("ja tinha falado uma vez mas nao ficou
escrito"). Toda venda do site tem DUAS pontas independentes, e nao pode
confundir uma com a outra:

1. **BAIXA FISICA — `EstoqueLoja` REAL.** Quando o webhook do Pagar.me
   confirma o pedido PAGO (`loja_pagamento._marcar_pago`, linha ~279),
   `_baixar_estoque` desconta o `EstoqueLoja.quantidade` DE VERDADE, pelo
   MOTOR UNICO (`loja_estoque_reserva.consumir` → `baixa_venda.aplicar_venda`
   canal `'site'`). A loja debitada vem de `_loja_baixa(pedido)`
   (`loja_pagamento.py:67`): **entrega/express** baixa de `loja_origem_site()`
   (`AppConfig.loja_site_estoque_id`, default "Loja Anesio Pinto Rosa");
   **retirada** baixa da loja ESCOLHIDA pelo cliente (`loja_retirada_id`).
   Essa e a MESMA linha `EstoqueLoja` mostrada/editada em
   `/admin/loja-online/catalogo` (`loja_catalogo._estoque_site_map`, le
   `loja_origem_site()`) e no seletor de `/pedidos/estoque-loja` — logo a
   venda do site aparece refletida nas duas telas. Tolera shortfall (registra
   `venda_site_sem_estoque`, nunca trava). NUNCA fazer a venda do site deixar
   de descontar o `EstoqueLoja` fisico.

2. **DISPONIBILIDADE NO FRONT — plano-do-dia, NUNCA o EstoqueLoja fisico.**
   O que o cliente ve como "esgotado / pode comprar" vem UNICAMENTE do
   plano-do-dia (`EstoqueSitePlano`, tela `/admin/loja-online/plano-do-dia`,
   servico `loja_plano_dia.py`), por `(kind, item_id, data_de_entrega)`.
   `loja_catalogo._saldo_para_dia` (linha ~141) decide so pelo plano;
   `qtd_planejada` e setada MANUALMENTE pelo dono (default 99999 = sem
   limite). Fail-open: sem plano cadastrado o item vende livre — o plano so
   serve pra CAPAR/zerar itens especificos naquele dia. Isso e o "setup
   diferente" que existe DE PROPOSITO: o site pode vender o que a loja ainda
   nao produziu (planeja no futuro), entao a disponibilidade NAO pode olhar
   o estoque fisico. No pagamento, alem da baixa fisica, o plano tambem
   incrementa `qtd_reservada` (`_reservar_no_plano_do_dia`, linha ~280) —
   camada independente da baixa fisica.

**CONSEQUENCIA OPERACIONAL (o que o dono precisa saber na pratica)**: editar
o estoque da loja — em `/pedidos/estoque-loja` OU no campo de estoque do
`/admin/loja-online/catalogo` — **NAO muda o que o cliente ve/pode comprar no
site**. A vitrine olha SO o plano-do-dia. Mexer no `EstoqueLoja` altera o
ledger fisico (de onde a venda desconta) e o numero exibido no catalogo admin,
mas a disponibilidade do front continua igual. Pra **esgotar ou liberar** um
item no site naquele dia, o gesto e no **plano-do-dia**
(`/admin/loja-online/plano-do-dia`, `qtd_planejada`), nunca no estoque da loja.

**Resumindo**: venda do site = SEMPRE desconta `EstoqueLoja` fisico da loja
de origem/retirada (visivel no catalogo e no estoque-loja) E a vitrine so
mostra o que o plano-do-dia libera. Sao camadas separadas — nunca fundir,
nunca a disponibilidade do front passar a depender do estoque fisico, nunca
a venda do site parar de baixar o fisico. Cancelamento/reembolso espelha as
duas (`_estornar_estoque` + `_devolver_ao_plano_do_dia`). Nuances aceitas:
o catalogo exibe DISPONIVEL (`quantidade - reservada`) enquanto
`/pedidos/estoque-loja` mostra o fisico (`quantidade`) — mesma linha, bases
diferentes; e pedido de RETIRADA em loja != origem baixa a loja escolhida
(reflete em `/pedidos/estoque-loja` da loja escolhida, nao no catalogo do
site). Testes: `tests/test_loja_estoque_vitrine.py`,
`tests/test_loja_estoque_reserva.py`, `tests/test_loja_online_vendas.py`.

## B2B — baixa na SEPARACAO + orcamento aprovado VIRA venda (regras do dono, 07/07/2026)

Tres regras ditadas pelo dono, escritas na pedra:

1. **"Estoque so pode ser baixado quando o pedido e separado pelo padeiro
   na tela do /padeiro."** Venda B2B com `data_entrega` NAO baixa
   `EstoqueProducao` na criacao — baixa quando o padeiro clica SEPARAR
   (`padeiro/routes.py::separar_b2b` → `vendas_b2b.baixar_na_separacao`).
   Marcador: `VendaB2B.estoque_baixado_em` (NULL = aguardando separacao).
   Venda IMEDIATA (sem data de entrega, nunca passa pelo padeiro) continua
   baixando na criacao. Reverter separado→pendente (`reverter_status_entrega`)
   estorna; re-separar baixa de novo (idempotente pelo marcador). Editar/
   sincronizar data segue o regime (`sincronizar_baixa_com_data`: pendente
   que GANHA data estorna; que PERDE data baixa). Backfill one-shot em
   `migrations_legacy` marcou as vendas antigas (que baixaram na criacao)
   como ja-baixadas — sem isso o padeiro baixaria em DOBRO. NUNCA regredir
   pra baixa na criacao nem baixar sem checar o marcador.
2. **Demanda pendente e COMPROMETIDO, nao estoque**: vendas ativas ainda nao
   baixadas entram no balanco da industria
   (`previsao_producao.balanco_industria`, linha "Vendas B2B" no breakdown;
   cesta explode via `componentes_de_cesta`) e o disponivel exibido nas telas
   de venda/copilot = fisico − `vendas_b2b.comprometido_b2b_pendente()`.
3. **Orcamento leve, aprovacao AMARRADA**: fazer orcamento aceita linha
   livre e sem data; APROVAR exige tudo (`orcamentos.validar_para_aprovacao`:
   data de entrega, todo item com FK do catalogo, quantidade inteira,
   desconto/frete zerados — "embuta nos precos") e CRIA a VendaB2B na hora
   (`_converter_em_venda`; `Orcamento.venda_id` vincula; cliente mensal sem
   parcela, demais parcela unica). Aba "Aprovados" do dashboard = aprovados
   com `venda_id IS NULL` (legado pre-regime).

Endurecimento pos-revisao (08/07/2026): CLAIM atomico (UPDATE condicional,
padrao do Confirmar do Slack) em TODOS os caminhos de baixa
(`vendas_b2b._claim_baixa`: separar, sincronizar data, reabrir) e na
aprovacao do orcamento; aprovacao persiste claim + venda + vinculo num
commit UNICO (`criar_venda(commit=False)`).

**Baixa de componente de cesta na PROPRIA linha (fix 08/07/2026)**:
componente receita/produto debita a linha dele no `EstoqueProducao`
(antes componente produto caia numa linha anonima all-NULL); componente
MP debita `MateriaPrima.estoque_atual` + `MovimentacaoEstoque` 'saida'
com referencia `Venda B2B #<id> ` (`_baixar_componente_mp`; movimento so
do que SAIU — estorno via `_saldo_mp_baixado` devolve exato, entrada com
`preco_unitario=None`). `comprometido_b2b_pendente` espelha (receita +
produto contam; MP fora — nao aparece no estoque_map).

**Rascunho arquivavel (08/07/2026)**: `Orcamento.arquivado_em` (idioma do
`Receita.arquivada_em`; ALTER em `migrations_legacy` PG+SQLite). Arquivar
= so rascunho (`orcamentos.arquivar/desarquivar`, rota toggle
`/b2b/orcamentos/<id>/arquivar`); some de Pendentes, aparece na aba
Arquivados com badge, nao transiciona status ate desarquivar. 'recusado'
segue significando "cliente disse nao" — nao reusar pra rascunho morto.

Testes: `tests/test_b2b_baixa_separacao.py`,
`tests/test_orcamento_aprova_vira_venda.py`; regressao reescrita em
`test_b2b.py`/`test_b2b_copilot.py`/`test_orcamentos.py` (arquivar
incluso).

## Copilot (servico) — canais: Slack + WhatsApp do dono. SEM interface web

`app/services/copilot.py` orquestra tools com Claude Sonnet 4.6 (Anthropic API).
Prompt caching ativo: `system` + ultima tool com `cache_control: ephemeral`
(cache breakpoint cobre ~95% dos tokens de input — custo cai ~90% apos o
primeiro request da janela de 5min).
Tools: criar_pedido, receber_mp, ajuste_estoque, mudar_status_pedido, criar_fornecedor,
marcar_ponto, criar_tarefa, marcar_tarefa_feita, balanco_congelados, entrada_lote_loja,
registrar_desperdicio,
consultar_pedido/estoque/fornecedores/margem/funcionario/caixa/foco/tarefas/vendas_itens/desperdicio/cartinhas.

Tools de write requerem aprovacao. Tool nova = adicionar em `TOOLS` +
`PAPEIS_POR_TOOL` (teste trava sem a entrada explicita) + executor read em
`_EXECUTORES_READ` quando for leitura — TODOS os canais herdam na hora.

**UI WEB REMOVIDA em 10/06/2026 (decisao do dono).** Nao existe mais
`app/blueprints/copilot/`, `copilot.js` nem o FAB lateral — NAO recriar.
Testes em `tests/test_remocao_copilot_web.py` travam regressao. Os canais
vivos sao:

1. **Slack** (`app/services/slack_bot.py`): DM/@mention → `copilot_svc.
   interpretar`; writes com botao Confirmar/Cancelar (Block Kit).
2. **WhatsApp do dono — DIRETO PELO Z-API, sem n8n** (`app/services/
   zapi_bot.py` + blueprint `zapi_bot`): webhook `POST /zapi/webhook?k=
   ZAPI_BOT_WEBHOOK_TOKEN` configurado no painel Z-API. So responde pro
   `ZAPI_BOT_DONO_NUMERO` (whitelist hard). **Read-only**: chama
   `interpretar(apenas_leitura=True)` — Claude NEM VE as tools de write
   (decisao do dono: zero edicao/acao pelo WhatsApp). Historico persistente
   em `ZapiBotConversa` (80 turnos), aceita imagem. Idempotente por
   messageId (`ZapiBotEventoProcessado`).

O n8n foi APOSENTADO — `app/blueprints/bot/routes.py` e API legada
(`/api/bot/faturamento`, token `BOT_API_TOKEN`); nao construir nada novo
nela, e nao confundir com o bot do dono (que e o zapi_bot acima).

## Chatwoot (atendimento omnichannel — WhatsApp + IG + site)

Self-hosted no Railway (projeto SEPARADO `positive-presence`;
`https://atendimento.opaopadariaartesanal.com.br`, Chatwoot 4.x + Valkey).
12 atendentes. O NOSSO sistema integra via API: bot de IA responde conversas
`pending` (webhook `POST /crm/bot?k=CHATWOOT_BOT_SECRET`), card do cliente,
vigias. Z-API (alertas internos do dono) e SEPARADO e intocavel.

**Env (Railway da padaria)**: `CHATWOOT_URL`, `CHATWOOT_ACCOUNT_ID`,
`CHATWOOT_API_TOKEN` (token de USUARIO, Profile Settings — le conversas/
inboxes/contatos), `CHATWOOT_BOT_TOKEN` (token do AGENT BOT, /super_admin →
Agent Bots — posta como bot), `CHATWOOT_BOT_SECRET` (segredo NOSSO, igual ao
`?k=` da Outgoing URL do Agent Bot), `CHATWOOT_CARD_TOKEN`,
`CHATWOOT_DATABASE_URL` (backup).

**LICAO DURA (12/06/2026)**: token de Agent Bot NAO pode LISTAR conversas
(GET /conversations = 401 mesmo com token VALIDO). So pode postar mensagem /
toggle_status. Consequencias ja corrigidas: (1) o diagnostico testa o bot
token com sonda `POST /conversations/0/toggle_status` — 404 = valido, 401 =
invalido; (2) leituras (`buscar_historico`, `listar_conversas_paradas`)
preferem o token de USUARIO com fallback pro de bot — com so o bot token, o
detector de abandono ficou CEGO EM SILENCIO por semanas (401 → lista vazia).
NAO regredir leituras pro token de bot.

**Diagnostico (owner-only)**:
- `GET /admin/debug-chatwoot` — servidor/latencia/versao, validade dos 2
  tokens (sem vazar valor; expoe len + flag parece_url), inboxes com
  `precisa_reautorizar` por canal, e `conclusao` pronta. `?conversa=N`
  adiciona erros de envio (erro bruto da Meta por mensagem falhada) +
  historico da conversa N.
- `GET /admin/debug-bot?busca=X` — VNDA (o que o BOT ve) vs EstoqueLoja
  (lojas fisicas) lado a lado.

**Vigia de infra**: cron 15min (`seru_cron`, lock 7739, `CHATWOOT_VIGIA_INFRA=0`
desliga) roda o diagnostico e alerta o dono via Z-API na transicao
saudavel→doente; re-alerta mesmo problema a cada 6h; avisa "✅ normalizou" na
recuperacao. Estado persistido em `AppConfig` (anti-spam sobrevive a deploy).

**Vigia do bot (chatbot_vigia)**: compara o que o bot disse com o CATALOGO DO
SITE (VNDA — MESMA fonte que o bot consulta). NUNCA comparar com EstoqueLoja:
bot vende pelo site; loja fisica e outra realidade (falso "bot delirou" em
12/06/2026: VNDA disponivel + 872 un na loja — bot estava certo).

**Alertas do Vigia no /entregas/painel (15/06/2026)**: alem do WhatsApp, os
alertas ALTA do vigia aparecem num BANNER pulsante com som "chato" (klaxon
WebAudio, reusa o `audioCtx` armado pelo "LIGAR PAINEL") no painel de
entregas. O som SO para quando alguem clica no banner (= reconhece,
server-side: silencia em todos os aparelhos). Aba lateral (drawer) lista o
historico com link da conversa no Chatwoot pra resolver. Fonte: VigiaVeredito
(alerta=True, gravidade='alta') — mesma do WhatsApp. Pendente = nao
reconhecido + dentro da janela (`VIGIA_PAINEL_JANELA_HORAS`, default 8h).
Backend: `chatbot_vigia.alertas_pendentes_resumo/reconhecer_pendentes/
historico_alertas/link_chatwoot`; colunas `reconhecido_em`/`reconhecido_por_id`
em VigiaVeredito. Rotas: resumo dobrado no `api_painel` (poll 20s) +
`POST /entregas/api/painel/vigia/reconhecer` + `GET .../vigia/historico`.

**Incidente 12/06/2026 (pos-mortem curto)**: IG caiu por token Meta expirado
(inbox `reauthorization_required` → Reauthorize resolveu; conectar com System
User do Business Manager evita expirar a cada 60d). WhatsApp "Falha ao
enviar" foi instabilidade DA META (~12h, erro generico "An unexpected error"
— classe code 2), passou sozinho. App Meta "O PAO ChatWoot" foi PUBLICADO em
08/06 — se mensageria falhar de novo, conferir "Acoes necessarias"/App
Review/verificacao da empresa no painel Meta.

## Chamar cliente pelo WhatsApp (painel de entregas, 11/07/2026)

Botão "💬 Chamar" em cada linha do modal "Pedidos do site" (drawer no
`/entregas/painel`, iframe `painel_pedidos.html`). Clicar dispara o TEMPLATE
aprovado pro cliente e abre a conversa no painel de atendimento à direita —
reusa o mesmo `postMessage {tipo:'vigia-abrir-conversa', conv_id, nome}` que o
vigia já usava pra abrir conversa (`painel.html:458` → `abrirThread`).

- **Por que template**: fora da janela de 24h a Meta só deixa a EMPRESA
  iniciar com template aprovado (utilidade). Pedido do site normalmente não
  tem conversa aberta no WhatsApp, então precisa do template.
- **Backend**: `POST /entregas/api/atendimento/chamar-cliente` (recebe
  `codigo`, acha o `PedidoOnline`, usa telefone+nome). Orquestração em
  `chatwoot.iniciar_conversa_whatsapp(telefone, nome, params=[nome, codigo])`:
  acha/cria contato pelo telefone → reusa conversa aberta na inbox do WhatsApp
  (não duplica thread) ou cria uma nova → **SEMPRE manda o template** (fix
  11/07: conversa "aberta" no Chatwoot ≠ janela de 24h aberta na Meta — o
  skip em conversa reusada deixava o cliente sem receber NADA; utilidade
  dentro da janela não custa, fora custa centavos). A mensagem vai com
  `content` renderizado (CHATWOOT_WHATSAPP_TEMPLATE_CORPO, default = o texto
  recomendado) — sem content, versões do Chatwoot mostram balão vazio na
  thread. Tudo com o **token de USUÁRIO** (`CHATWOOT_API_TOKEN`; o de Agent
  Bot nem lista conversa). Erro NÃO é silenciado (devolve o corpo cru da
  Meta pra depurar). O fetch do botão manda `X-CSRFToken` + retry único via
  `/auth/csrf-token` em `csrf_expirada` (padrão do autosave do cronograma).
- **Config (env)**: `CHATWOOT_WHATSAPP_INBOX_ID` (id da inbox do WhatsApp) +
  `CHATWOOT_WHATSAPP_TEMPLATE` (nome do template aprovado, 2 vars: {{1}}=nome,
  {{2}}=código) + `CHATWOOT_WHATSAPP_TEMPLATE_LANG` (default `pt_BR`). Faltando
  qualquer um = botão devolve aviso, não quebra o painel. Diagnóstico owner:
  `GET /entregas/api/atendimento/chatwoot-inboxes` lista as inboxes pra achar
  o id.
- **Pré-requisito humano**: criar+aprovar o template de utilidade na Meta
  (não dá pra fazer por código). Payload do template em
  `chatwoot.enviar_template` (`processed_params` posicional) — se a versão do
  Chatwoot reclamar do formato, é aqui que ajusta.
- Testes: `tests/test_chamar_cliente_whatsapp.py` (orquestração com requests
  mockado + endpoint + guardas).

## Portal Wi-Fi cativo das lojas (12/07/2026, Ribeiro do Vale)

Kit TP-Link Omada (EAP610 + OC200) na loja Ribeiro do Vale; SSID aberto
`O_Pao_Clientes`. Pedido do dono: coletar e-mail válido + WhatsApp válido +
senha criada pelo cliente + ANIVERSÁRIO, e o cliente sair LOGADO no site
(conta criada na hora; "se já tiver conta, resolver a questão"). Fluxo
desenhado pra contornar o mini-navegador do captive portal (CNA não
compartilha cookies com o navegador real): o link de login one-time viaja
pelo WHATSAPP e abre no navegador de verdade.

- **Fluxo**: `GET /loja/wifi` (form standalone: nome, e-mail, WhatsApp,
  senha, dia/mês obrigatórios + ano opcional, aceite LGPD) → `POST
  /loja/wifi/cadastrar` cria `WifiPortalSessao` (código `WIFI-XXXXXX`,
  senha já hasheada — texto puro nunca persiste) → tela
  `/loja/wifi/validar/<token>` mostra botão wa.me com a mensagem pronta
  ("Ativar Wi-Fi O Pão — código WIFI-XXXXXX") + polling em
  `/loja/wifi/status/<token>` → cliente ENVIA a mensagem (validação de
  posse do telefone pelo fluxo GRATUITO iniciado pelo cliente — sem custo
  de template) → interceptor no webhook do Chatwoot responde com o link
  `/loja/wifi/entrar/<login_token>` (one-time, 30 min) que loga a sessão
  de cliente da loja (`loja_auth.login_cliente`) e manda pra `loja.home`.
- **Validação forte (12/07/2026, pedido do dono após o 1º teste)**: nome
  exige DUAS palavras (nome + sobrenome); e-mail = formato estrito +
  detector de typo de provedor (`_typo_de_provedor`, Damerau distância 1
  de gmail/hotmail/uol etc. — pega gmial.com mesmo squatted) + domínio
  existente via DNS (`_dominio_email_resolve`: MX com fallback A/AAAA,
  dnspython no requirements; fail-open DELIBERADO em erro de INFRA de
  DNS — instabilidade nunca barra cadastro no balcão, só NXDOMAIN/sem
  registro reprova); WhatsApp = celular BR real (`_whatsapp_valido`: DDD
  da lista ANATEL + nono dígito 9 + 8 dígitos, com/sem o 55). Nos testes
  a fixture autouse `_sem_dns` patcha a checagem de domínio (offline e
  determinístico) — a função REAL é capturada no import do arquivo ANTES
  do patch (senão o teste de fail-open exercitaria o lambda da fixture).
- **Interceptor** (`crm/routes.py::bot_webhook`): código Wi-Fi FURA o gate
  de `pending` (funciona em conversa 'open'), resposta determinística SEM
  Claude, e `definir_status('resolved')` SÓ se a conversa estava pending
  (nunca fecha conversa de atendente). Regex `RE_CODIGO_WIFI` (alfabeto
  sem 0/O/1/I).
- **4 regras de conta** (`wifi_portal._resolver_conta`; posse provada = o
  telefone que ENVIOU a mensagem): (a) tudo novo → cria conta + loga;
  (b) e-mail existe + telefone bate → login SEM pedir a senha antiga
  (aprovado pelo dono; a senha do form é IGNORADA — nunca sobrescrever);
  (c) telefone pertence a OUTRA conta → loga nela (e-mail mascarado na
  resposta); (d) e-mail existe + telefone diverge → NÃO loga: magic link
  pro e-mail cadastrado (Postmark) e o link NUNCA vai no WhatsApp. Guest
  (senha_hash NULL): upgrade se o telefone bate ou sem histórico
  divergente; senão magic link (protege pedidos antigos). E-mail sem
  prova NUNCA loga em conta alheia — não regredir.
- **Aniversário no `Cliente`**: `aniversario_dia`/`aniversario_mes`/
  `nascimento_ano` (ALTER em `migrations_legacy` deployado ANTES do
  modelo — procedimento de 2 commits, sonda `/api/claude/deploy`).
- **Enforcement Omada** (`app/services/omada.py`): Open API do OC200 via
  nuvem (client_credentials + `extPortal/auth`, authType 4). Best-effort:
  sem `OMADA_*` envs configuradas o cadastro funciona igual (só não
  autoriza o rádio — fase de teste roda por link direto). Envs:
  `OMADA_API_URL`, `OMADA_CLIENT_ID`, `OMADA_CLIENT_SECRET`,
  `OMADA_OMADAC_ID`, `OMADA_SITE_ID`; resultado fica em
  `wifi_autorizado_em`/`wifi_erro` na sessão. Diagnóstico owner:
  `GET /admin/debug-omada` (presença das envs + token;
  `?autorizar_mac=<MAC>` autoriza um aparelho de teste por 60 min).
- **Env obrigatória pro fluxo**: `WIFI_PORTAL_WHATSAPP` (número do
  WhatsApp do atendimento em dígitos com 55, ex `5511...`) — vazio, a
  tela instrui envio manual em vez do botão wa.me.
- **PII**: sessões >30 dias são podadas em `criar_sessao`; sessão expira
  em 30 min; `aceite_lgpd_em` NOT NULL (checkbox obrigatório no form).
- Testes: `tests/test_wifi_portal.py` (16 casos). ARMADILHA de teste: o
  marker `loja_host` vai SÓ nos testes de rota `/loja/wifi` — no arquivo
  inteiro ele derruba o `/crm/bot` (em host de loja só `/loja/*` responde).
- **TRAVA DURA POR VOUCHER (decisão do dono 12/07/2026, após a descoberta
  abaixo)**: o portal do OC200 roda no modo **Voucher** (nativo, sem API):
  a janelinha pede um código, e quem entrega o código é o NOSSO fluxo — o
  dono gera o lote no **Hotspot Manager** do Omada (uso único, duração
  longa ex. 90d), exporta e sobe em `/admin/wifi-vouchers` (owner; link na
  área Administração); cada cadastro validado no WhatsApp consome UM
  voucher (`wifi_portal.alocar_voucher`, claim atômico via UPDATE
  condicional) e o código vai na resposta junto do link de login. Estoque
  vazio = fluxo segue sem mencionar código (pré-enforcement). Estoque
  abaixo de `WIFI_VOUCHER_AVISO_MIN` (default 50) → WhatsApp ao dono
  (`_avisar_estoque_baixo`, dedup 24h em AppConfig). Modelo `WifiVoucher`
  (db.create_all). Cliente recorrente sem voucher: refaz o cadastro →
  regra (b) loga direto e ganha voucher novo.
- **ARMADILHA de teste do conftest (descoberta 12/07/2026)**: o fixture
  `app` mantém um app context PUSHADO o teste inteiro; requests do test
  client REUSAM esse contexto (Flask só empilha outro se o app for
  diferente), então `g` — incluindo o cache `g._login_user` do
  Flask-Login — é COMPARTILHADO entre requests do MESMO teste. Request
  anônima antes de request logada = a logada herda o anônimo em cache e
  dá 403 falso. Por isso os testes de rota separam "exige login" e "caso
  logado" em FUNÇÕES diferentes — manter assim.
- **DESCOBERTA 12/07/2026 (fase 2 travada no OC200)**: o gateway de nuvem
  `*-omada-northbound.tplinkcloud.com` só conhece controladores
  CLOUD-BASED (CBC) — token com OC200 devolve `-7131 Controller ID not
  exist` MESMO com credenciais/omadacId certos (omadacId é o parâmetro
  `omadacId=` na URL do controlador, NÃO o deviceId). A Open API do OC200
  é LOCAL ("Interface Access Address", ex. https://192.168.15.3:443),
  inalcançável do Railway (LAN da loja/CGNAT Vivo); a TP-Link chegou a
  anunciar REMOÇÃO da Open API do OC200 no v5.15 por hardware fraco
  (fórum oficial; OC300 mantém). Caminhos possíveis: (a) portal nativo
  click-through do próprio OC200 com Redirect URL pro /loja/wifi (sem
  API, libera no "aceitar", cadastro vira redirect obrigatório mas não
  trava); (b) migrar pro Cloud-Based Controller da TP-Link (licença
  anual/dispositivo — o código atual funciona SEM MUDANÇA, é o gateway
  que já integramos); (c) software controller em VPS público; (d)
  port-forward pro OC200 (NÃO recomendado: expõe admin + CGNAT).
  Decisão do dono pendente.
- **CAMINHO ESCOLHIDO (dono 13/07/2026): LOGIN via RADIUS.** O dono quis
  trava dura por LOGIN ("cliente preenche os dados ou faz login pra acessar
  o wifi"). O External Portal seamless esbarra no certificado do OC200 local
  (o navegador do cliente teria que confiar no HTTPS auto-assinado do
  controlador — integradores confirmam "instale um cert válido"; + risco de
  DNS rebinding no roteador da Vivo). O RADIUS resolve SEM esse problema
  porque é o OC200 que SAI perguntando (saída pela internet, CGNAT não
  bloqueia). Arquitetura em 2 peças:
  - **Endpoint `POST /api/wifi/radius-check`** (`app/blueprints/wifi_api/`,
    CSRF isento, Bearer `WIFI_RADIUS_TOKEN`): valida e-mail+senha contra
    `Cliente` (ativo + tem_conta + check_senha). Anti-enumeração (mesma
    resposta pra senha errada e conta inexistente) + rate limit. Responde
    503 sem a env. Roda no gestão.opao (host gestão, NÃO os hosts de loja —
    `/api/wifi` não é `/loja/*`, então a ponte chama a URL do gestão).
  - **Ponte RADIUS** (`wifi_radius/bridge.py` + README): script standalone
    ZERO-dependência (só stdlib), roda num VPS (o Railway não expõe UDP).
    Decripta o User-Password (PAP, RFC 2865), chama o endpoint, devolve
    Access-Accept/Reject com Message-Authenticator (RFC 3579) +
    Response Authenticator. Fail-closed (erro = Reject). `--selftest`
    valida a cripto sem rede. DOIS segredos distintos: `WIFI_RADIUS_SECRET`
    (OC200↔ponte) e `WIFI_API_TOKEN`=`WIFI_RADIUS_TOKEN` (ponte↔gestão).
  - Cliente cria conta em `/loja/wifi/criar` (página LEVE standalone, sem o
    `_base.html` — no captive portal os scripts externos de GA/FB Pixel
    ficam pendurados e TRAVAM a janelinha; por isso a página do site normal
    não serve). `wifi_portal.criar_conta_direta`: e-mail novo cria na hora;
    e-mail que JÁ existe (conta OU convidado) → `ja_existe`, NÃO cria/reivindica
    nada e NÃO manda e-mail. PRIVACIDADE (decisão do dono, caso "esposa
    ciumenta digita o e-mail do marido e vê que ele comprou cesta pra
    vizinha"): reivindicar conta de convidado por e-mail sozinho exporia o
    histórico — só pelo site (/loja/cadastrar) com verificação no e-mail.
    A página do portal (portal_omada.html) redireciona pra `opao.online`
    (LANDING_URL) ao conectar, não pra página em branco. Pre-Auth Access
    (walled garden) precisa liberar `opao.online`.
    Config Omada: RADIUS Profile (IP do VPS:1812 + secret) + portal auth =
    RADIUS (PAP) + Local Web Portal + Import Customized Page.
  - **Página customizada** `wifi_radius/portal_omada.html`: sobe no OC200
    (Portal → Design → Import Customized Page), roda LOCAL no controlador
    (sem cert), tem login email+senha + botão clicável "Criar conta" →
    opao.online/loja/cadastrar (resolve o "ninguém digita URL" do dono). O
    login POSTa `/portal/radius/auth` (authType 2) MESMA origem. `AUTH_URL`/
    `AUTH_TYPE` no topo do script são os pontos a ajustar no teste na loja
    (a tela mostra o errorCode/msg bruto pra guiar). VPS (216.238.102.67)
    criado no Vultr SP; `setup.sh` (scratchpad) instala a ponte com os 2
    segredos.
  - Testes: `tests/test_wifi_radius.py` (endpoint + cripto da ponte).

## Bot de atendimento — hardening 02/07/2026 (4 pacotes)

Pesquisa de melhorias no bot/vigia/auditor aprovada pelo dono virou 4
pacotes, todos implementados. Testes: `tests/test_bot_melhorias_0702.py`.

**06/07/2026 (caso Simone, auditor)** — `tests/test_handoff_dedupe.py`:
- **Dedupe de handoff**: conversa ja transferida ha < 90 min
  (`HANDOFF_DEDUP_MIN`) nao ganha 2º "vou te passar pra equipe" — webhook e
  vassoura trocam por `TEXTO_HANDOFF_REPETIDO` (status ainda vai pra 'open',
  idempotente; acao vira 'handoff_repetido', sem 2º registro). Marcador
  `handoff_em` gravado no store (`salvar_historico(..., handoff=True)`,
  preservado na reconstrucao do JSON — sem isso o dedupe morre no turno
  seguinte).
- **Valores rotulados**: `consultar_pedido` (site) devolve `subtotal_itens`,
  `frete` e `preco_unit` por item + instrucao `como_apresentar`; prompt exige
  "itens R$X + frete R$Y = total R$Z". O alerta do vigia sobre "R$138 vs
  R$148" era falso positivo de VALOR (4x34,50+10=148, conta exata — conferi
  preco_site na API), mas a APRESENTACAO sem rotulo era real. O "pedido
  exibido sem numero" e o bot lembrando o pedido DA PROPRIA cliente do
  historico da conversa (exibicao de pedido e fail-closed por telefone/CPF
  em `bot_tools._consultar_pedido_online`/`_autorizar_pedido`).

**P1 — Graves (cliente no vacuo)**:
- Msg SO de audio/anexo nao suportado: resposta deterministica pedindo texto
  (antes: return silencioso e a conversa ficava presa em `pending` pra
  sempre — o followup nao dispara quando a ultima msg e do cliente). Grava
  '[cliente enviou audio/anexo nao suportado]' no store.
- Excecao no processamento: envia `chatbot.FALLBACK_TEXTO` ao cliente antes
  de abrir a conversa (antes ia pra fila humana EM SILENCIO).
- `anthropic.Anthropic(timeout=..., max_retries=1)` em TODAS as chamadas de
  bot/vigia/auditor — o default do SDK (~10min) segurava thread + lock da
  conversa quando a conexao travava.
- Injection: "responda como" so casa roleplay ("como se fosse", "como um") —
  "responda como faco pra pagar?" era falso positivo real que virava handoff.
- **Vassoura** (`chatbot.varrer_pendentes_sem_resposta`, roda no cron do
  followup): conversa `pending` com ultima msg do CLIENTE ha 10-720min =
  bot ficou devendo (thread daemon morta em deploy; a idempotencia ja
  committada impede o Chatwoot de reentregar o webhook). Responde e
  destrava, 5/ciclo. Kill-switch `CHATBOT_VASSOURA=0`.

**P2 — Handoff (caso Elaine)**:
- Prompt: recusa de oferta != pedido de humano != fim de conversa — pergunta
  "posso ajudar com mais alguma coisa?" e encerra; NAO transfere.
- **Enforcement em codigo**: 1ª tentativa de transferir SEM nenhuma consulta
  antes e sem motivo de excecao (alergia/reclamacao/humano/estorno/
  reembolso/cancelamento) e RECUSADA 1x via tool_result mandando consultar;
  se o modelo insistir, o handoff sai (nunca loop). ARMADILHA:
  `_handoff_excecao` olha SO `motivo`/`resumo` — NUNCA `mensagem_cliente`
  (quase todo handoff diz "um atendente vai continuar" nela; olhar la
  anulava o enforcement inteiro — pego por teste).
  **'cartinha' SAIU das excecoes em 06/07/2026** (auditor: 5/8 handoffs
  preguicosos, 2 de cartinha/pos-compra): `consultar_pedido` agora devolve
  o TEXTO da cartinha (so pro dono autorizado do pedido) + secao
  "POS-COMPRA E CARTINHA" no prompt — o bot confirma sozinho; MUDAR a
  cartinha continua indo pro humano.
- `stop_reason == 'max_tokens'`: refaz 1x com teto 2400 (antes link/preco
  cortado ia pro cliente).
- Followup: dedupe so conta `enviado_whatsapp=True` (envio que falhou nao
  suprime a retentativa); cutucao enviado e mesclado no ultimo turno
  assistant do store (a API nao aceita 2 assistant seguidos).

**P3 — Custo** (o vigia roda em TODA resposta = maior volume de IA):
- Short-circuit do vigia: fechamento trivial ("ok", "obrigada" —
  `_e_fechamento`) sem handoff nao gasta modelo.
- Cache: `PROMPT_VIGIA`/`PROMPT_ABANDONO` com `cache_control ephemeral`.
  O auditor NAO tem cache DE PROPOSITO: execucoes com horas entre si
  (7/9/12/15/19h) e TTL de 5min = pagaria o premio de escrita (1.25x) sem
  nunca ler de volta.
- **Debounce/coalescing de rajada** (`crm/routes`): cada webhook deposita a
  msg em `_PENDENTES` e a thread dorme `CHATBOT_DEBOUNCE_S` (default 4s;
  0 sob PYTEST_RUNNING); quem acorda drena TUDO e responde UMA vez —
  cliente que quebra a frase em 3 baloes = 1 chamada Opus, nao 3.

**P4 — Auditor v2 + vigia**:
- Regra UNICA de "handoff preguicoso": `chatbot_vigia.handoff_foi_preguicoso`
  (transferiu sem NENHUMA tool de leitura fora transferir/encerrar). O
  detector de compra do vigia e o agregador do auditor delegam a ela.
  `tools_usadas=None` (registro de bot antigo) = NAO acusa (sem dado).
- Dedup de ALTA: 2ª ALTA da MESMA conversa dentro de 2h registra o veredito
  (banner do painel segue) mas nao re-manda WhatsApp (`_alerta_alta_recente`;
  so conta `enviado_whatsapp=True`; fail-open — erro na consulta deixa o
  alerta sair).
- Auditor recebe dados REAIS em vez de inventar: `por_hora` (histograma de
  eventos — pico so com dado), `funil_site` (`PedidoOnline` criados/pagos/
  cancelados + faturamento por `pago_em`, nao por status) e
  `comparativo_dia_anterior` no resumo das 19h (tendencia vs ontem).
- Tom (regra do dono): auditor e o BALANCO FRIO — sem 🚨/panico (alarme em
  tempo real e papel do vigia); amostra < 10 conversas nao manchete
  porcentagem, usa numeros absolutos.

## Contas a Pagar (NF/boleto via Slack → IA → Dropbox → banco)

Feature de 2026-05-23. Funcionarios postam foto de NF/boleto em canais Slack de
recebimento; o bot **so le** (nunca posta), a IA extrai os dados e cria uma
`ContaPagar`.

- **Modelo**: `app/models/financeiro.py::ContaPagar` (Numeric(10,2) pra dinheiro;
  `slack_file_id` unique = idempotencia). `db.create_all` cria a tabela.
- **Extrator**: `app/services/conta_pagar_ia.py::extrair_documento` — Claude
  vision, **Sonnet primeiro, Opus no fallback** se faltar campo critico
  (valor/fornecedor/codigo de barras). Aceita imagem e PDF (document block).
  Modelos via env `OCR_MODELO_SONNET`/`OCR_MODELO_OPUS`.
- **Captura**: `app/services/conta_pagar_slack.py::processar` — sobe a imagem pro
  Dropbox (`upload_publico`) ANTES de extrair (nao perde o doc se a IA falhar).
  Interceptado em `slack_bot.processar_evento_mensagem` (canal em
  `SLACK_CANAIS_NF` → handler silencioso, sem vinculo, sem copilot). O webhook
  `slack/routes.py` foi ajustado pra deixar passar canais de NF (alem dos
  `SLACK_CANAIS_PERMITIDOS`).
- **Tela**: `/contas-pagar` (admin) — abas Em aberto/Pagos/Ignorados, detalhe
  editavel (a IA so chuta; humano corrige), marcar pago, vincular fornecedor e
  ligar NF↔boleto. Link na sidebar (Catalogo, owner-admin).
- **Historico**: botao "Importar historico (30d)" (owner) varre
  `conversations_history` dos canais em background. Idempotente por file_id.

**Config no Slack App (necessaria pra captura funcionar)**:
- Scopes: `channels:history`, `channels:read`, `files:read` (+ reinstalar app).
- Event subscription: `message.channels`.
- Bot **membro** dos 3 canais.
- Env `SLACK_CANAIS_NF` = CSV dos IDs dos canais. `ANTHROPIC_API_KEY` e Dropbox
  ja configurados (reusados do copilot/entregas).

## Incidente 12/07/2026 — revert em bloco + regras que ficaram

Uma sessao paralela pushou ~100 commits que derrubaram producao: implementou
o "M6 Commit D" (drop das colunas BLOB) SEM confirmar o deploy do commit 1,
junto com upgrade em massa de requirements e healthcheck novo. Um container
BOOTOU (migrations rodam no startup), dropou receita.imagem_blob,
produto.imagem_blob, foto_recebimento.imagem e pedido_item_foto.imagem, e o
deploy NUNCA foi promovido — o codigo antigo no ar selecionava as colunas
dropadas e TODA tela com esses modelos virou 500. Restauracao: commit
5934d137 (volta a arvore ao ultimo deploy saudavel + re-cria as 4 colunas
vazias; sem perda de dados — a guarda do drop so rodava com coluna vazia).
Salvage triado em seguida (vigia custo IA, dead-man backup, ajustes de
acuracia, teste do QR de conferencia). REGRAS QUE FICARAM:

- **Sicredi NAO se mexe sem ordem explicita do dono** (12/07/2026: "nos ja
  fazemos os boletos da sicredi e ele estava mexendo nisso"). As mudancas
  da sessao revertida em sicredi_cnab/cobrancas foram DESCARTADAS.
- **Upgrade de dependencias so isolado e com ordem**: nunca junto de
  mudanca de schema/feature (o upgrade em massa foi descartado).
- **Sonda `GET /api/claude/deploy`** (Bearer CLAUDE_API_TOKEN) devolve o
  commit no ar (RAILWAY_GIT_COMMIT_SHA) — o procedimento de 2 commits de
  schema DEVE confirmar o deploy do commit 1 por ela antes do commit 2.
- **M6 Commit D segue PENDENTE**: refazer só pelo procedimento canonico
  (drenar BLOBs pelo card /admin/debug-schema ate 0 pendentes → commit 1 =
  codigo para de ler/escrever BLOB, atomico, deploy confirmado →
  commit 2 = DROP guardado). O mapa detalhado esta no parecer da triagem
  (historico ate 6c89d5a0).
- healthcheckPath no railway.json: NAO reaplicar sem decisao do dono — nao
  previne a classe de incidente (migrations mutam schema antes da
  promocao) e ha risco de 301 do redirect HTTPS congelar deploys.

## Vigias novos (12/07/2026, resgatados da sessao revertida)

- **Vigia de custo de IA** (`app/services/uso_ia_vigia.py`): cron 1h
  (lock 7748, kill-switch `USO_IA_VIGIA=0`) soma o gasto de HOJE (00:00
  BRT) em `UsoIA` e alerta o dono no WhatsApp quando passa do teto
  `USO_IA_TETO_DIA_USD` (default US$ 25) — transicao + re-alerta 6h +
  normalizacao, estado em AppConfig. Sob demanda: `GET /admin/vigia-uso-ia`
  (owner; `?alertar=1` roda com WhatsApp). Testes: tests/test_uso_ia_vigia.py.
- **Dead-man do backup** (`seru_cron`): marcos `backup_ultimo_run_em`/
  `backup_ultimo_ok_em` (+ variantes chatwoot) persistidos em AppConfig; o
  heartbeat Slack diario avisa se o ultimo OK tem >28h ou se o job roda mas
  nunca deu OK. Card Backup do /admin/debug-schema mostra "Ultimo OK"
  separado do ultimo run. Testes: tests/test_backup_deadman.py.

## Cobrancas Sicredi (boleto hibrido via CNAB 400) — homologacao em curso

Gestao de boletos das parcelas B2B direto no sistema (04-06/07/2026). Banco
748 (Sicredi Vale do Piquiri); contatos: Luiz Henrique (homologacao),
Marines F. Kisler (validacao dos arquivos).

- **Tela**: `/cobrancas` (admin; link no macro `financeiro`). Fluxo: parcela
  B2B -> "Gerar cobranca" (snapshot do pagador do ClienteB2B) -> completar
  endereco+CEP -> marcar -> "Gerar remessa" (`REMnnnnn.CRM`) -> subir no
  Sicredi -> upload do RETORNO da baixa (liquidacao quita a parcela B2B
  junto) e traz o QR Pix (registro tipo 8).
- **Services**: `app/services/sicredi_cnab.py` (remessa/retorno CNAB 400,
  nosso numero AA+B+NNNNN+DV mod11, sequenciais nunca repetem) e
  `app/services/sicredi_boleto.py` (fase 2: codigo de barras 44 pos, campo
  livre §10.3, DV geral mod11 §10.5, fator de vencimento §10.7 com ciclo
  novo pos-22/02/2025, linha digitavel mod10 §10.8.3, PDF com ITF "2 de 5
  intercalado" 103x13mm a 0,5cm da margem + QR Pix quando
  `pix_copia_cola` chegar). Manuais completos extraidos:
  `scratchpad/cnab400.txt` do container (se sumir, pedir os PDFs ao dono).
- **Fixtures OFICIAIS travadas em `tests/test_cobrancas_sicredi.py`**:
  DV nosso numero (ag 0101/posto 19/benef 00207/21-1-03527 -> 5) e linha
  digitavel do boleto-modelo do banco
  `74891.12115 03527.501013 19002.071041 6 85810000018000` — o pipeline
  inteiro (campo livre, DV geral, mod10) reproduz essa linha. NAO mexer nas
  formulas sem os fixtures passarem.
- **Config por env** (defaults no `_cfg()`): SICREDI_AGENCIA=0726,
  SICREDI_POSTO=61, SICREDI_BENEFICIARIO=34325, SICREDI_CNPJ, SICREDI_BYTE=2,
  SICREDI_BENEF_NOME (nome impresso no boleto — CONFIRMAR razao social).
- **Homologacao (06/07/2026)**: 1a remessa DEVOLVIDA pelo banco com 1 ajuste:
  `enderecoPagador` (275-314 do detalhe) e OBRIGATORIO e foi vazio. Fix:
  `validar_para_remessa` agora recusa endereco vazio; tela edita endereco
  inline; acao "voltar pra pendente" (status remessa/rejeitada -> pendente,
  MANTEM nosso numero) permite corrigir e gerar NOVA remessa (novo
  sequencial). Titulo REGISTRADO nao volta pra pendente (dessincronizaria
  com o banco — precisa instrucao de baixa, ainda nao implementada).
  Proximo passo do dono: corrigir endereco da cobranca de homologacao,
  gerar remessa nova + boleto PDF e mandar pra Marines validar.

## Slack Bot (copilot via DM/@mention)

Bot reutiliza 100% das tools do copilot. DM direta ou @mention em canal permitido
dispara o mesmo `copilot_svc.interpretar` — single-workspace.

**Env vars** (Railway):
- `SLACK_BOT_TOKEN`: xoxb-... (Bot User OAuth Token)
- `SLACK_SIGNING_SECRET`: assinatura HMAC pra validar webhooks
- `SLACK_CANAIS_PERMITIDOS`: CSV de IDs de canais publicos (ex `C012,C034`). Vazio = so DM.

**Setup do Slack App** (https://api.slack.com/apps):
- Bot scopes: `chat:write`, `im:history`, `im:write`, `app_mentions:read`, `users:read`
- Event subscriptions: `message.im`, `app_mention`. Request URL: `<host>/slack/events`
- Interactivity: ON. Request URL: `<host>/slack/interact`
- Install no workspace, copia tokens pro env

**Fluxo**:
1. `/slack/events` (POST) valida signing + idempotencia (`SlackEventoProcessado`),
   dispara processamento async via ThreadPoolExecutor (ack <3s).
2. `slack_bot.processar_evento_mensagem`: resolve `SlackVinculo` (slack_uid → Usuario),
   carrega `SlackConversa` (multi-turn), chama `copilot_svc.interpretar(text, user, historico)`.
3. Resposta:
   - Write tool → cria `SlackAcaoPendente` (token unico) + posta Block Kit preview com botoes
   - Read tool → posta texto direto
   - Conversa → posta texto
4. `/slack/interact` (POST) recebe clique. Resolve token → SlackAcaoPendente.
   Confirmar → `copilot_svc.executar` + `chat.update` com resultado. Cancelar → marca cancelado.

**Seguranca**:
- HMAC-SHA256 do signing secret obrigatorio (rejeita >5min de delta = replay)
- `SlackVinculo.ativo=True` obrigatorio — sem vinculo, bot recusa
- Token de acao expira em 10min e so quem pediu pode confirmar (`acao.slack_user_id == clicker`)
- Canais publicos: lista branca via `SLACK_CANAIS_PERMITIDOS`
- CSRF isento no blueprint (Slack nao envia o token CSRF; autenticidade vai pela signing)

**UI admin**: `/slack/install` lista vinculos + form pra criar novos (slack_user_id ↔ Usuario).

## Cadastro assistido por IA (08/07/2026)

Pedido do dono: colar print/lista de itens novos (nome + preco) e a IA
propor o cadastro de Produtos usando os PARECIDOS ja cadastrados como
referencia de composicao (ex: "MISTO CRANBERRY" herda a estrutura do
"MISTO" trocando o pao). Tela `/produtos/cadastro-ia` (admin; link na
area Catalogo), service `app/services/cadastro_ia.py`.

- **Fluxo**: upload de imagem (JPG/PNG/WebP <=8MB) OU texto colado →
  `analisar()` manda catalogo atual (produtos+componentes, receitas, MPs;
  caps com aviso, nunca truncar em silencio) + a lista pro Sonnet 4.6
  (`CADASTRO_IA_MODELO` pra override; custo em UsoIA funcao=
  'cadastro_ia') → tabela de REVISAO editavel (checkbox por item e por
  componente, nome/preco/categoria/qtd) → `salvar_lote()` grava.
- **A IA so propoe, o banco manda**: `_sanitizar_proposta` re-resolve
  id inventado por nome exato; componente inexistente so vira NOVO se
  for MP (criada com custo 0 + aviso "definir custo no Banco de MPs");
  receita/produto novos NUNCA sao criados automaticamente — viram
  `ProdutoItem` orfao (mesmo destino da tela de composicao, resolve em
  `/produtos/cestas/orfaos`). Produto homonimo e PULADO no salvar.
- **Preco**: seletor por lote (whitelist `CAMPOS_PRECO` = preco_site |
  preco_atacado | preco_loja — decisao do dono: escolher na tela).
  `item_nome` sempre espelha o nome do alvo (FK manda, nome e fallback).
- NADA e salvo sem revisao humana: componente errado = baixa de estoque
  errada no motor de vendas. Testes: `tests/test_cadastro_ia.py`
  (Anthropic sempre mockada). Tela validada a 390px (metodo Playwright
  + Bootstrap local via `npm pack bootstrap@5.3.3` — o registry npm
  passa pelo proxy; a CDN continua bloqueada).

## Planejamento assistido por IA — pedidos + producao (08/07/2026)

Pedido do dono ("criar uma IA para fazer pedido para as lojas e para a
producao... colocar o opus 4.8"); escolha dele: botao NAS TELAS (nao
Slack/cron), producao so propoe (ENVIAR segue humano). Service
`app/services/planejamento_ia.py`; modelo **Opus 4.8** (excecao
consciente a padronizacao Sonnet, decisao do dono; env
`PLANEJAMENTO_IA_MODELO`). Custo em UsoIA ('pedido_loja_ia' /
'producao_ia'). A IA SEMPRE propoe POR CIMA dos motores deterministicos
— ela nao inventa a conta, ajusta com contexto (calendario/feriados que
o modelo conhece — nao ha tabela de datas especiais) e justifica.

- **Pedidos loja→industria**: botao "Sugerir por IA" por loja nas DUAS
  telas de pedidos da semana (rota unica `POST /producao/pedidos-semana/
  ia`, param `modo`): `/pedidos-semana/media` (modo='media', base =
  grade da MEDIA, contraprova = venda+estoque) e `/pedidos-semana/
  estoque` (modo='venda', 11/07/2026 a pedido do dono: base = motor
  VENDA+ESTOQUE com o `seguranca` da tela, contraprova = media; itens
  identificados por `item_key` porque a grade inclui MPs — 'mp:<id>').
  Contexto = os dois motores + estoque + desperdicio 7d. O JS (fonte
  unica `producao/_pedidos_ia_js.html`, `{% set ia_modo %}` antes do
  include) preenche a grade EDITAVEL (celula amarela = mudou; celula
  travada/ja-pedido NUNCA e tocada — sanitizado no server E pulado no
  JS) + painel com motivo por item e parecer. NADA e criado: o pedido
  continua nascendo pelos botoes Gerar de sempre (aplicar_grade →
  rascunho pendente).
- **Producao**: botao "Analisar por IA" em `/telaindustriateste` (rotas
  `POST /telaindustriateste/ia-proposta` e `/ia-aplicar`). Contexto =
  cronograma + alertas de falta + pendencias do padeiro (agendado/
  vencido) + fornadas por dia. Proposta = AJUSTES de celula (receita ×
  data → qtd) com motivo; linha de RETORNO e de INSUMO ficam fora
  (contexto e whitelist). Aplicar (checkbox por ajuste) grava via
  `cronograma_edit.editar_celula` — override de RASCUNHO com todas as
  guardas (dia bloqueado etc.). NUNCA chama aprovar/enviar plano —
  enviar ao padeiro e gesto humano (regra do dono 04/07 preservada).
- Sanitizacao espelhada nos dois lados: id/data fora do motor caem;
  qtd inteira >= 0; proposta >3x o motor ganha aviso visivel.
- Testes: `tests/test_planejamento_ia.py` (Anthropic sempre mockada).

## Varredura mobile (06/07/2026)

Pedido do dono ("versao very mobile"); escolha dele: varredura responsiva
geral priorizando lojas (pedidos/estoque), relatorios/PDV/financeiro e
producao/cronograma. Validada com Playwright a 390px (login real, Bootstrap
servido local — a CDN e bloqueada no sandbox e SEM ela a auditoria mente).

- **BUG CRITICO ACHADO NO CAMINHO**: o `@media print` da linha ~1969 do
  `style.css` NUNCA fechava (sobra da remocao do copilot web em 10/06) +
  um `@media 480px` orfao — o navegador tratava as ultimas ~700 linhas do
  CSS como regra de impressao (command palette e ajustes mobile antigos
  nunca aplicaram). Corrigido. Se mexer em CSS grande, validar balanco de
  chaves (o incidente passou 3 semanas invisivel).
- **Globais** (valem pra TODA tela, atual e futura): `app.js` embrulha
  tabela solta em `.mobile-table-scroll` (rola no lugar, nunca estica o
  body — era o estouro de /pedidos, /pedidos/congelados, /pdv/itens-vendidos);
  no mobile `.main-content .d-flex` quebra linha (barras de botoes/filtros);
  `.nav-tabs` vira trilho horizontal rolavel; `.form-control/.form-select`
  com 16px (iOS nao da zoom ao focar); celulas de tabela compactas.
- **`.dica-recolhe`**: explicador longo recolhido no mobile com "toque para
  ler tudo" (JS alterna `.aberta`). Aplicada no cronograma e nas duas telas
  de pedidos da semana — usar em qualquer explicador grande novo.
- O grid do cronograma ja tinha 1a coluna sticky + scroll proprio
  (`.crono-wrap`) — o estouro era o form de filtros sem wrap.

**Varredura TOTAL (06/07/2026, "faz em todas as telas")**: as 146 rotas
GET-HTML do sistema (extraidas do url_map + fichas parametrizadas) foram
medidas a 390px — TODAS fecham sem estourar o body. Fixes alem dos globais:
btn-group com wrap no mobile (b2b/orcamentos), grid dos dashboards com
`minmax(min(440px,100%),1fr)`, barra do widget de atendimento do painel de
entregas com wrap (container e filtros), header do /padeiro com wrap +
`body{overflow-x:hidden}` (drawer fixo com transform cria scroll fantasma
no Chrome mobile) e `.proj-tarefa` com wrap (linha de tarefa dos projetos).
Ao criar TELA NOVA: validar a 390px antes de fechar (metodo: Playwright +
login real + `scrollWidth > clientWidth` = estourou; no sandbox o Bootstrap
precisa ser servido LOCAL porque a CDN e bloqueada).

## Sidebar

Secoes (`sidebar-section-title`) sao **colapsaveis** — JS adiciona chevron + persiste
estado em `localStorage` por nome. Implementacao em `app/static/js/app.js`.

### Hub de areas + navegacao de fonte unica (02/07/2026)

A tela inicial do admin (`main/home.html`, so `is_admin()`) mostra CARDS por
area; clicar abre `/area/<slug>` (`main.area`), uma pagina que LISTA as funcoes
daquela area. Antes cada card era link direto pra 1 rota.

**Fonte unica dos links (NAO duplicar)**: os links de cada area vivem SO em
`app/templates/_area_nav.html` (um macro por area: `lojas`, `producao`,
`catalogo`, `vendas`, `financeiro`, `rh`, `relatorios`, `administracao`,
`fichas`, + dispatcher `render(slug, variant)`). Dois consumidores:
- **sidebar** (`base.html`): cada secao chama `{{ areanav.<area>('sidebar') }}`
  (classe `sidebar-link` + estado ativo por `request.path`).
- **pagina da area** (`main/area.html`): `{{ areanav.render(slug, 'area') }}`
  (classe `area-link`).
Ao adicionar/remover uma funcao, mexa SO no macro — os dois lugares atualizam
juntos. Guardas de permissao por link sao os mesmos de antes.

**Metadados da area** (titulo/icone/cor/descricao/permissao) ficam em
`app/nav.py::AREAS` (fonte unica dos cards + do guarda da rota `/area/<slug>`).
`areas_visiveis(user)` filtra por permissao; `area_por_slug(slug)` resolve a
pagina. A rota espelha o guarda do card (403 sem permissao, 404 slug invalido).

**Import com contexto**: quem usa o macro faz
`{% import "_area_nav.html" as areanav with context %}` (precisa de
`current_user`). **Armadilha**: a secao **Fichas** renderiza pra usuario
ANONIMO (fica fora do bloco `is_authenticated` na sidebar — pagina 404 publica
da loja). Por isso o macro `fichas` guarda `current_user.is_authenticated and
current_user.is_admin()` (AnonymousUser NAO tem `.is_admin()`). Ha teste
travando o render anonimo; as demais areas so rodam autenticadas.

**Garantia de zero regressao**: a sidebar nova (macro) foi comparada link-a-link
com a antiga em 47 paths (3619 links) — byte-identico. Testes em
`tests/test_area_hub.py`.
