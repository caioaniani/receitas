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

## Stack

Flask 3 + SQLAlchemy + Bootstrap 5 + Postgres em prod / SQLite local.
Padaria Opão: receitas, pedidos, entregas, PDV, estoque, RH, copilot (Claude Sonnet 4.6).

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
`RETENCAO_LOGS_DIAS=365` (NFLog, VigiaVeredito),
`RETENCAO_CONVERSAS_DIAS=180` (ChatbotConversa inativa),
`RETENCAO_EVENTOS_DIAS=7` (idempotencia Slack/Zapi),
`RETENCAO_BACKUPS_DIAS=90` (dumps Dropbox — decisao do dono;
o dump MAIS RECENTE de cada pasta nunca eh apagado).
Desligar: `RETENCAO_AUTO=0`. Inspecao manual: `GET /admin/retencao`
(dry-run) / `?executar=1` (apaga). NUNCA adicionar tabela de negocio
(pedido/venda/estoque) nos alvos — retencao eh so log/contexto/
idempotencia.

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

## Integracao Seru (PDV)

**Documentação**: https://integration.plataformaseru.com.br/v1/docs.
Credenciais: `SERU_CLIENT_ID` + `SERU_CLIENT_SECRET` no env Railway.

**Service base**: `app/services/seru.py` (OAuth2 client_credentials, token cache,
paginacao por dia em paralelo). `data_local()` converte UTC → BRT.

### Fase 1 — Relatorio
- Rota `/pdv/itens-vendidos` + API `/pdv/api/itens-vendidos`
- Service `app/services/vendas_itens.py` agrega por produto com match fuzzy local
- Tool copilot `consultar_vendas_itens` (read, admin+gerente)

### Fase 2 — Auto-baixa estoque
Mapeamentos persistentes em 3 tabelas:
- **`SeruProdutoMap`**: nome Seru → receita/produto + estado (mapeado/ignorado/pendente)
  + `fator_quantidade` (Float, default 1.0) pra produtos compostos
  (ex: "NOZES COM MANTEIGA" = 2 fatias / 10 por pao = fator 0.2)
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

## Copilot

`app/services/copilot.py` orquestra tools com Claude Sonnet 4.6 (Anthropic API).
Prompt caching ativo: `system` + ultima tool com `cache_control: ephemeral`
(cache breakpoint cobre ~95% dos tokens de input — custo cai ~90% apos o
primeiro request da janela de 5min).
Tools: criar_pedido, receber_mp, ajuste_estoque, mudar_status_pedido, criar_fornecedor,
marcar_ponto, criar_tarefa, marcar_tarefa_feita, balanco_congelados, entrada_lote_loja,
registrar_desperdicio,
consultar_pedido/estoque/fornecedores/margem/funcionario/caixa/foco/tarefas/vendas_itens/desperdicio.

Tools de write requerem aprovacao (preview HTML no chat). Frontend em
`app/static/js/copilot.js` — modal lateral com textarea, Enter envia, Shift+Enter
quebra linha.

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

## Sidebar

Secoes (`sidebar-section-title`) sao **colapsaveis** — JS adiciona chevron + persiste
estado em `localStorage` por nome. Implementacao em `app/static/js/app.js`.
