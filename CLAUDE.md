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
- **Ausência numa sonda NÃO é prova de ausência.** Antes de usar uma sonda/
  consulta como evidência de que "X não existe", diga PRIMEIRO qual é a
  cobertura dela — o que ela enxerga e o que fica de fora. Sonda que cobre
  um subconjunto só prova presença, nunca ausência. Caso real (03/08/2026):
  afirmei "zero pedidos pro dia 09, é a verdade do banco" com base no
  `/api/claude/cronograma`, que só enxerga demanda firme de item SOB
  ENCOMENDA — cesta comum vendida pro dia 9 era invisível ali por desenho.
  Havia dezenas de pedidos.
- **Quando o usuário diz "os dados existem, eu estou vendo" e a minha
  verificação diz que não, o default é que a MINHA verificação está errada
  ou parcial.** O dono conhece a operação dele; investigar a divergência
  vem ANTES de explicar a observação dele como engano/cache/tela velha.
  Só concluir "o que você vê não é real" com prova positiva (reproduzido,
  com `arquivo:linha` do defeito) — nunca por eliminação.
- **Duas visões da mesma coisa discordando é a PISTA, não ruído.** Mapa
  mostrando pedidos e lista dizendo zero (03/08/2026) = cada um vem de uma
  fonte; a obrigação é rastrear QUAL endpoint alimenta cada um antes de
  declarar um dos lados "residual/cosmético". No caso real, o lado que
  descartei ("pinos velhos") era o dado CERTO (`/api/rotas` corrigido) e o
  lado em que confiei (lista) vinha de um endpoint ainda cego
  (`/api/atribuidos`). Inventei um bug cosmético pra fechar a história em
  vez de seguir a contradição — e entreguei diagnóstico errado ao dono
  duas vezes seguidas.

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
- `GET /api/claude/acuracia?dias=&motor=` (16/07/2026): resumo do painel de
  acuracia + WAPE por (loja, receita) dos motores vivos — pro assistente
  diagnosticar de fora onde a previsao erra.
- `GET /api/claude/drivers?todos=1` (07/08/2026): motoristas de entrega
  (nome, telefone, ativo, capacidade, tem_token/tem_pin — NUNCA o token/PIN
  em si). Criada pra confirmar o seed dos motoristas do Dia dos Pais
  (`migrations_legacy._seed_drivers_entrega`, marker
  `seed_drivers_entrega_2026_08`): cadastro em massa que o dono manda por
  WhatsApp entra por seed one-shot (match por nome sem acento/caixa OU
  `telefone_chave`; nunca sobrescreve dado do dono, telefone so preenche se
  vazio; grava ja com DDI 55 — `normalizar_telefone` nao adiciona o 55 no
  envio Z-API). Testes: `tests/test_seed_drivers.py`.
- `GET /api/claude/vendas-snapshot?dias=&loja=&pedidos=1` (18/07/2026):
  faturamento POR DIA do snapshot `VendaSeruDiaLoja` (itens vs total dos
  pedidos) + lista ao vivo dos pedidos com a diferenca total−itens. Criada
  no caso "card Por loja do /pdv/ mostra Nebraska R$10.355 mas foi
  R$3.327": NAO era bug — 23 cobrancas "PDV Facil" SEM itens (so valor,
  R$7.028,50) entraram na company Nebraska em 17/07 (3 no dia 16, R$113 —
  comportamento novo; suspeita: operacao Bread & Brew, que parou de vender
  na propria company apos 15/07). `faturamento_pedidos` (card/bot) conta
  essas cobrancas; `faturamento_itens` (relatorio de produtos) nao — e
  venda sem item NAO baixa estoque nem entra na previsao de demanda.
- Testes: `tests/test_claude_api.py`.

## Cockpit do dono — briefing diario + home + manual (16/07/2026)

Pedido do dono ("nao estou conseguindo pilotar o aviao"): o sistema cresceu
mais rapido que a capacidade de operar; quase tudo era "pull" (lembrar de
abrir tela). Tres pecas, UMA fonte de dados
(`app/services/briefing_dono.py`):

- **Briefing do dono, SOB DEMANDA** (`GET /admin/briefing`, owner;
  `?enviar=1` manda pro WhatsApp dele): vendas de ontem por loja vs media
  do mesmo dia-da-semana (fonte `VendaSeruDiaLoja.faturamento_pedidos` —
  inclui kit/box; site por `pago_em`; companies Seru AGRUPADOS pela Loja
  vinculada — Bread & Brew + Filial Nebraska = UMA linha, dono 17/07),
  pendencias de decisao e custo de IA de ontem.
  **O envio automatico das 07:00 foi REMOVIDO em 17/07/2026 a pedido do
  dono ("nao quero receber") — job `briefing-dono` apagado do seru_cron,
  lock 7750 liberado-reservado. NAO reagendar sem ordem explicita.**
- **Bloco "Precisa de voce hoje" na home do admin** (`main.index` →
  `home.html`): as MESMAS pendencias, com link por item. Itens de tela
  owner-only (orfaos de cesta, PDV) so aparecem pro owner.
- **Vendas TOTAIS na home (17/07/2026, pedido do dono)**: painel "💰 Vendas"
  na home, SO pro dono (faturamento = cockpit pessoal, mesmo gate do
  /admin/briefing), com DUAS secoes: **HOJE em destaque** (parciais —
  `vendas_hoje()`, snapshot que o cron de 15min recaptura; SEM delta de
  proposito: dia incompleto vs dia cheio daria "-60%" falso a manha toda) e
  **Ontem** (total geral PDV+site, PDV com delta vs a **SEMANA PASSADA** —
  chaves `total_geral`/`comparado_com`/`comparado_com_label`/`pdv_base`/
  `pdv_delta_pct`/`snapshot_ok` em `vendas_ontem()`), site e linhas por loja
  compactas.
  **COMPARACAO = MESMO DIA-DA-SEMANA, 7 DIAS ANTES (dono 23/07/2026)**:
  "sexta faturou X vs a sexta passada". SUBSTITUIU a media das ultimas 6
  ocorrencias do dia-da-semana — o dono quer comparar com UM dia concreto,
  nao com media (`_DIAS_COMPARACAO = 7`; a chave por loja virou `base`, nao
  `media`, porque o valor e de um dia so). Base ausente ou ZERO => delta
  `None` (nao existe % sobre zero; a tela mostra "sem comparacao"). Loja que
  vendeu na semana passada e ZEROU ontem continua aparecendo com -100%.
  REGRA: a home chama `vendas_hoje()`/`vendas_ontem(capturar=False)` — le
  SO o snapshot do banco e NUNCA bate na API Seru; sem snapshot de ontem a
  home AVISA em vez de mostrar R$ 0 falso (`snapshot_ok`). O briefing/
  WhatsApp ganhou a linha "Total". Testes travam o gc.assert_not_called()
  da home; NAO usar 'Vendas de ontem' como marcador de teste do painel (a
  sidebar tem um title= com esse texto — falso positivo ja aconteceu).
  **Cancelamentos + descontos do dia (21/07/2026, pedido do dono)**: cada
  painel (hoje/ontem) mostra "🚫 Cancelados: N · R$ X | 🏷️ Descontos: R$ Y"
  do PDV Seru. Fonte SO-SNAPSHOT (`VendaSeruDiaBreakdown`, sem API): a
  captura (`vendas_diarias.capturar_periodo`) agora guarda, alem da CONTAGEM
  de cancelados (dimensao 'cancelados', chave ''), o VALOR (chave 'v', soma
  do `total` dos cancelados) e o DESCONTO (dimensao 'desconto', chave '',
  soma do `discount` top-level da API — R$, so das vendas NAO canceladas).
  Helper `cancelamentos_descontos_do_banco(di, df)` le os dois eixos;
  `vendas_hoje`/`vendas_ontem` expoem `cancelados_n`/`cancelados_valor`/
  `desconto`. ARMADILHA fechada: o reader `vendas_pdv_do_banco` DISTINGUE
  chave '' (contagem, `int`) de chave 'v' (valor) — sem isso o dinheiro do
  cancelado entraria como numero de pedidos. Dia capturado antes das linhas
  novas devolve 0 gracioso (contagem de cancelados sempre existiu). Sonda
  `vendas-snapshot` ganhou `subtotal`/`desconto` em pedidos_ao_vivo.
  Testes: secao "Cancelamentos (valor) e descontos" em
  `tests/test_vendas_diarias.py` + `test_vendas_{hoje,ontem}_inclui_
  cancelamentos_e_descontos` em `tests/test_briefing_dono.py`.
  **ABRIR o detalhe (drill-down AO VIVO, 21/07/2026, escolha do dono via
  AskUserQuestion "pedidos individuais ao vivo")**: a linha virou BOTAO
  (`.abrir-cd` em `home.html`) que abre um modal com o detalhe pedido-a-
  pedido do dia — cancelados (hora, loja, valor, caixa, tem NFC-e) e
  descontos (hora, loja, subtotal, desconto, total). EXCECAO CONTROLADA a
  regra "home nunca bate na API": o detalhe pedido-a-pedido NAO existe no
  snapshot (so agregados por dimensao), entao SO ESTE caminho — no CLIQUE
  explicito, nunca no render da home — consulta o Seru. Servico
  `briefing_dono.cancelados_descontos_detalhe(dia)` (read-only, usa
  `_nf_autorizada` do vigia + `_resolver_loja_seru` pra loja; desconto so de
  NAO-cancelado); rota `GET /admin/vendas/cancelados-descontos?dia=`
  (`@owner_required`, restrita a hoje/ontem = 400 fora, Seru fora = 502
  gracioso). Modal Bootstrap com `esc()` em TODO texto externo (loja/caixa/
  codigo/erro vem da API) + `table-responsive`. Testes: secao "Drill-down"
  em `tests/test_briefing_dono.py`.
- **Manual de operacao** (`GET /admin/manual`, admin): o que roda sozinho /
  diario / semanal / mensal e de quem e cada gesto, com links.
  **REGRA DE PROCESSO**: toda funcao nova se registra no manual NA MESMA
  mudanca que a cria (quem opera, quando, onde aparece o lembrete) — se
  ninguem opera, nao se constroi (devolver a pergunta ao dono em vez de
  construir).

Pendencias cobertas (queries baratas, EXISTS/COUNT): ordem de hoje
(rascunho nao enviado / ausente — booleano `enviado_ao_padeiro`, nunca
`status`), producao vencida (falta>0 nao dispensada), orcamentos B2B
parados/aprovados-sem-venda, contas a pagar vencidas, estoque
`nome_pendente` (industria+lojas), orfaos de cesta, mapeamentos PDV
(`pdv_saude.contar_pendencias`), vigias doentes (chaves AppConfig
`*_quebrado_desde`/`*_estourado_desde` + `alertas_pendentes_resumo`).
Testes: `tests/test_briefing_dono.py`.

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
  - **xdist LIGADO no CI em 09/08/2026** (`pytest -n auto`): a suite passou
    de ~2400 pra ~3900 testes e o sequencial nao cabia mais no "Wait for
    CI". ARMADILHA resolvida no conftest: o processo CONTROLADOR do xdist
    importa o conftest primeiro e seta `DATABASE_URL` no `os.environ`; os
    workers HERDAM a env e "respeitavam" o arquivo do controlador — todos
    no mesmo SQLite = `table usuario already exists` em massa (1678 erros).
    O marcador `_PADARIA_TEST_DB_AUTO` distingue env auto-setada (worker
    sobrescreve com o slot proprio) de env do dev (segue respeitada).
    CONSEQUENCIA: rodar `pytest -n N` com `DATABASE_URL` fixado na mao
    volta a colidir — pra paralelo, deixe a env vazia.
  - **Banco isolado POR PROCESSO (17/07/2026)**: o topo do `conftest.py` agora
    da a CADA processo pytest seu proprio SQLite em `tempfile.gettempdir()`
    (chave = worker do xdist, senao `pidNNNN`), respeitando `DATABASE_URL`
    setado de proposito. Antes, sem xdist, TODO processo caia no
    `~/.padaria/padaria.db` FIXO. **Armadilha que isso fecha**: rodar DOIS
    pytest ao mesmo tempo (ex: full-suite em background + um arquivo em
    foreground pra debugar) fazia os dois baterem no MESMO arquivo — o reset
    por DELETE + recriacao do admin no startup de um apagava/duplicava as
    linhas do outro NO MEIO dos testes, gerando falhas NAO-DETERMINISTICAS e
    espalhadas (`StaleDataError`, `UNIQUE usuario.login`, linhas que "somem",
    ja vi de 3 a ~379 falhas por corrida). So aparece com pytest concorrente;
    CI (1 processo) fica verde e escondia. Efeito colateral bom: a suite nao
    dropa/limpa mais o `padaria.db` LOCAL do dev. Se ver falha nao-repetivel na
    suite, cheque `ps aux | grep pytest` ANTES de suspeitar do codigo.
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

**Endpoints de ENTREGA da era VNDA repontados (03-04/08/2026)**: a familia
inteira que montava "pedidos do dia" via `vnda.buscar_pedidos_do_dia` ficou
CEGA pro site apos o cutover — com o VNDA aposentado, o `erro` do client
derrubava a resposta e os pedidos do SITE nunca entravam no pool. Casos
reais do dono: "/rotas nao mostra nada do site" e "mapa da aba Operacao com
pinos reais e a LISTA dizendo nenhum pedido" (mapa vinha do /api/rotas ja
corrigido; a lista vem do /api/atribuidos, que nao). REGRA: todo consumidor
de `buscar_pedidos_do_dia` TOLERA o erro (base vazia) e injeta o site via
`_pedidos_online_do_dia`; retirada fica FORA da roteirizacao (/api/rotas)
mas DENTRO do inventario (/api/atribuidos, /api/produtos). Os 9 chamadores
cobertos: api_rotas, api_atribuidos (lista da aba Operacao), api_produtos
(aba Produtos), resetar_atribuicoes_dia (devolvia 500), _painel_pedidos_do_
dia, api_pedidos (entregas), imprimir por codes, driver api_pedidos (dava
502) e driver api_debug. Endpoint NOVO de pedidos-do-dia deve nascer desse
padrao. Testes: secao "/rotas e /driver enxergam pedido do SITE" em
`tests/test_rastreio_entrega.py`.

**Payload MAGRO da aba Operacao + kill-switch VNDA_PEDIDOS (04/08/2026,
pedido do dono "nessa tela nao precisa de cartinha e item")**:
- `_serializar_pedido_online(p, detalhes=True)` ganhou modo magro
  (`detalhes=False`): `itens=[]` e `cartinha_vnda=''`, mantendo codigo/
  destinatario/telefones/e_presente/divulgacao/endereco/data/periodo/
  expresso/retirada/status. `_pedidos_online_do_dia(target, detalhes=True)`
  espelha e, no modo completo, faz `selectinload(itens).selectinload(
  componentes)` (matou o N+1 de 150 pedidos x itens x componentes do menu).
- MAGRO: `api_atribuidos` (lista da Operacao; o card nao renderiza
  cartinha — conferido no JS antes de cortar), `api_rotas`,
  `resetar_atribuicoes_dia`, driver `api_debug`. COMPLETO (nao regredir):
  painel (`_painel_pedidos_do_dia`), `api_pedidos` (aba legada tem o
  EDITOR de cartinha), `api_produtos` (conta itens), imprimir e driver
  `api_pedidos` (motorista/cozinha separam pelo item).
- **XLSX da aba Produtos (08/08/2026, dono na vespera do Dia dos Pais)**:
  `GET /entregas/produtos.xlsx?data=&janela=...` — a agregacao de
  `api_produtos` virou o helper `_produtos_do_dia(target, janelas)` (fonte
  UNICA da aba e do export; o servidor RE-agrega, mesma regra da impressao
  por codes). Gerador `app/services/entregas_xlsx.py` (openpyxl). CONTRATO
  ajustado pelo dono no 1º uso real (mesmo dia): **UMA aba so** — "Vendidos
  no dia" e "A produzir" ABAIXO (a 2ª aba passava despercebida no celular)
  — e **SEM valores em R$** ("ninguem precisa saber dos valores, so eu"; a
  planilha circula com a equipe de montagem, dinheiro fica nas telas do
  dono — ha teste travando a ausencia de preco). Totais de producao POR
  UNIDADE — g e un nao se somam. Botao "XLSX" no card da aba (entregas.js,
  href do eco d.data/d.janelas da API). Testes:
  `tests/test_entregas_produtos_xlsx.py`.
- **Card "ENTREGAS DO SITE" no /padeiro (08/08/2026, dono: "tela pra usar
  2x no ano, dia das maes e dia dos pais")**: o resumo da aba Produtos
  (Vendidos + A produzir) DENTRO da TV do padeiro, mesmo motor
  (`_produtos_do_dia`). Liga/desliga por AppConfig
  `padeiro_resumo_entregas` — botao so pra ADMIN na propria tela
  (`padeiro.resumo_entregas_toggle`); padeiro ve o card, nunca o botao.
  Alvo = AMANHA (padeiro produz na vespera); antes das 10h = HOJE
  (madrugada do evento monta as entregas do dia em voo —
  `_alvo_resumo`, funcao pura). Best-effort: erro no motor nunca derruba
  a TV (card some, admin ve aviso). Testes:
  `tests/test_padeiro_resumo_entregas.py` (7 casos).
- **Impressao SEMPRE re-busca por codes** (`entregas.js::
  imprimirSelecionados` virou GET `/entregas/imprimir?codes=...`): o POST
  de snapshot mandava o estado em memoria da aba — com a lista magra, o
  papel do motorista sairia SEM itens/cartinha. O servidor reconstroi
  completo (com `_aplicar_cartinhas`). NUNCA voltar ao snapshot POST
  enquanto a lista da Operacao for magra.
- `vnda.buscar_pedidos_do_dia` agora tem curto-circuito: env
  `VNDA_PEDIDOS` != '1' (default) devolve `{'pedidos': []}` SEM HTTP e SEM
  `erro` — mata os spinners de ate ~25s e o banner "Erro temporario ao
  buscar pedidos no VNDA" que a tela mostrava a cada load. Religar so com
  ordem do dono (VNDA aposentado).
- Testes: `test_lista_da_operacao_vem_magra`, `test_painel_segue_completo`,
  `test_impressao_por_codes_sai_completa` (cartinha so na via CLIENTE — a
  do motorista omite DE PROPOSITO, `imprimir.html` linha 3) e
  `test_vnda_pedidos_curto_circuito_por_default` em
  `tests/test_rastreio_entrega.py`.

**Rastreio de entrega por PROGRESSO + "Iniciar rota" (01-04/08/2026, Dia
dos Pais)**: dono trocou o Lalamove por motoristas contratados (~150
pedidos, janela 06:00-10:00 do 09/08) e pediu rotas prontas + cliente
acompanhando ao vivo. Decisoes dele (AskUserQuestion): **progresso,
SEM GPS** (GPS de navegador so funciona com a pagina aberta — com motorista
avulso metade dos mapas congelaria) e **e-mail automatico** na saida da
rota. **ETA REMOVIDO em 08/08/2026 a pedido do dono ("nao precisa estimar
o tempo de entrega, talvez somente a posicao")** — o cliente ve so "voce e
a Nª parada — faltam M"; `_eta_minutos`/`RASTREIO_MIN_POR_PARADA` sairam
do codigo. NAO reintroduzir previsao de horario sem ordem. Pecas:
- **`RotaInicio`** (driver_id+data unique, tabela nova via db.create_all):
  marco de "saiu pra rua". `rastreio_entrega.iniciar_rota(driver, dia)`
  idempotente (`emails_em` trava re-disparo) manda o e-mail "saiu para
  entrega" com link `LOJA_BASE_URL + /loja/pedido/<codigo>` (NUNCA
  `url_for(_external=True)` — quebra fora de request e aponta pro host
  errado) — best-effort POR pedido, um e-mail ruim nao trava os outros.
- **Botao "🚚 Iniciar rota"** na pagina do driver (`driver/index.html`;
  endpoint `POST /driver/api/<token>/iniciar-rota`). O `/pedidos` do driver
  devolve `rota: {iniciada, iniciada_em}` — o front alterna botao/selo.
  Confirm antes (dispara e-mail em massa).
- **`status_do_pedido(codigo)`** (nunca levanta; erro = 'em_preparo'):
  fases em_preparo / a_caminho (driver, parada N, faltam M — SEM `eta`
  desde 08/08/2026) / entregue (hora) / problema (atribuicao nao_entregue —
  a pagina NAO detalha o motivo, quem fala com o cliente e a loja).
  `AtribuicaoEntrega` NAO tem relationship com Driver — lookup por id.
- **BOT de atendimento instrui o rastreio (08/08/2026)**: `bot_tools.
  _consultar_pedido_online` (so AUTORIZADO — mesmo gate da cartinha)
  devolve `rastreio` (status_do_pedido) + `link_acompanhamento`
  (LOJA_BASE_URL/loja/pedido/<codigo>); passo 2b da secao RASTREAMENTO do
  `chatbot_prompt.py` manda o link + posicao na rota e PROIBE estimar
  horario. Testes: `test_consultar_pedido_traz_link_e_rastreio_sem_horario`
  (test_handoff_dedupe.py) + `test_secao_rastreamento_manda_o_link_...`
  (test_chatbot_faq_pilar_b.py — janela do teste vizinho alargada pra 3200,
  armadilha conhecida dos testes de prompt).
- **Pagina do cliente** (`loja/pedido_confirmado.html`): bloco "Acompanhe
  sua entrega" com 3 passos + texto por fase; estado inicial renderizado no
  SERVIDOR (rota `pedido_confirmado` passa `rastreio`; sem flash de
  loading) e polling de 30s no `/loja/pedido/<codigo>/status` (JSON ja
  expunha `out['rastreio']`). RETIRADA fica FORA (nao ha entrega); polling
  para em entregue/problema.
- **Foto OBRIGATORIA no entregue** (dono 01/08): `api_status` do driver
  recusa entregue com 0 fotos (422 `precisa_foto`); no front o botao sem
  foto abre a CAMERA direto e o upload marca entregue sozinho
  (`marcarAposFoto`, resetado em cancelamento/erro). O painel staff mantem
  a valvula de escape sem foto. "Nao entregue" NAO exige foto.
- **"PULAR ENDERECO" (dono 08/08/2026, "portaria nao quis receber... ele
  vai voltar")**: `AtribuicaoEntrega.pulado_em` (procedimento de 2 commits,
  sonda ?colunas= confirmada) + `POST /driver/api/<token>/pular` — exige
  >=1 FOTO (fachada/portaria = prova de visita), so status pendente, joga
  a `ordem` pro fim da rota do dia (max+1) e o pedido SEGUE pendente (NAO
  e nao_entregue, que e desfecho final). Entregue POS-pulo exige foto NOVA
  (`_fotos_pos_pulo`: tirada_em > pulado_em — a foto da portaria nao
  comprova entrega; o front usa `precisa_foto_nova` pra abrir a camera
  direto). Botao "⏭️ Pular endereco" no modal do driver (fluxo
  `pularAposFoto`, espelho do marcarAposFoto) + selo "pulado as HH:MM" no
  card. O cliente pulado cai pro fim da fila no rastreio sozinho (posicao
  = pendentes a frente). Testes: `tests/test_driver_pular.py` (7 casos).
- **Ensaio de carga 04/08** (scratchpad `ensaio_dia_dos_pais.py`, Google e
  e-mail mockados): 150 pedidos / 9 motoristas → /api/rotas 150ms (9 rotas,
  150 paradas, k-means real), salvar lote 179ms, /api/atribuidos 51ms,
  driver /pedidos 21ms, status_do_pedido 1.5ms/req (150 clientes em poll de
  30s ≈ 5 req/s — folga grande). Roteirizacao aguenta o dia.
- **POS-REVISAO (fixados 04/08)**: (1) iniciar rota SO NO DIA da entrega —
  a tela do driver abre ja na PROXIMA data com rota, e um clique de
  curiosidade na vespera mandaria "saiu para entrega" um dia antes E
  queimaria a idempotencia do dia real (endpoint recusa 422); (2) claim
  ATOMICO do disparo de e-mails (UPDATE condicional em `emails_em`, padrao
  Confirmar do Slack; falha catastrofica devolve o claim); (3) o e-mail de
  saida FILTRA status in (pago, em_preparo, a_caminho) — cancelado apos a
  rota salva nao recebe "saiu para entrega" (nada limpa a atribuicao no
  cancelamento) e divulgacao fica fora (e-mail placeholder); (4)
  `status_do_pedido` consulta o `PedidoOnline` — pedido entregue/a_caminho
  POR FORA da rota (painel staff, express/Lalamove) nao mostra mais
  "Entregue" no topo com "em preparo" no bloco (a_caminho sem rota sai sem
  parada/ETA, front mostra o generico); (5) regra "tem rastreio?"
  centralizada em `loja/routes._rastreio_do_pedido` (pagina + JSON, antes
  divergiam); (6) polling para em `cancelado`; (7) front do driver: guard
  de resposta em voo na troca de data + listener de apagar foto delegado
  1x (empilhava por modal aberto). ACEITO (cosmetico): `<style>` do bloco
  dentro do body.
- Testes: `tests/test_rastreio_entrega.py`. Manual de operacao registrado
  (QUANDO PRECISAR — "Dia de MUITAS entregas com motoristas proprios").

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
(motor unico — Sonnet 5 em todos os canais desde 05/08/2026).

### Modelos Anthropic em uso (atualizado 05/08/2026)

**PADRONIZACAO DO DONO (05/08/2026, "pode trocar todos para sonnet 5")**:
TODAS as funcoes de IA rodam **`claude-sonnet-5`** — bot Chatwoot, WhatsApp
do dono, copilot Slack, vigia, auditor, follow-up, OCRs (NF/boleto e
cupom), SEO, cadastro IA, planejamento IA, Google reviews, treino.
Substituiu a regra de 25/06 ("Sonnet 4.6 exceto bot/WhatsApp/OCRs =
Opus 4.8"). Motivacao: custo (Sonnet 5 = $3/$15 tabela, promo $2/$10 ate
31/08/2026; Opus 4.8 = $5/$25) com qualidade de geracao mais nova.
Testes que travam: `test_uso_ia.py::test_modelos_por_funcao` (+ os pinos
em test_chatbot_faq_pilar_b/test_copilot/test_conta_pagar_ia/
test_copilot_fork_canais).

**Regras da migracao pro Sonnet 5 (nao regredir)**:
- **Thinking**: o Sonnet 5 liga thinking ADAPTATIVO por padrao (omitir o
  param = pensa; o teto `max_tokens` cobre thinking + texto juntos).
  Politica adotada: chamadas COM tools (bot Chatwoot `chatbot.py`, copilot
  `copilot.py`) ficam com adaptativo (ajuda a usar tools; o bot subiu o
  teto pra 4000/retry 8000); chamadas SEM tools (vigia, auditor, followup,
  OCRs, SEO, reviews, treino, cadastro, planejamento) levam
  `thinking={'type': 'disabled'}` EXPLICITO — sao classificadores/
  extratores de teto curto onde thinking so comeria teto e custo.
- **Sampling**: `temperature`/`top_p`/`top_k` nao-default = 400 no
  Sonnet 5. Nenhum call site usa — NAO introduzir.
- **Tokenizador novo** (~30% mais tokens pro mesmo texto): tetos justos
  truncam; ao criar chamada nova, dar folga.
- **Extracao de resposta**: SEMPRE iterar `resp.content` filtrando
  `type == 'text'` (com adaptativo o primeiro bloco pode ser thinking —
  `content[0]` quebra). Todos os call sites ja fazem isso.
- **`uso_ia._PRECOS` tem a linha do sonnet-5 ($3/$15 tabela cheia de
  proposito — superestima ~30% ate 31/08, direcao segura pro vigia de
  custo)**. Modelo novo SEM linha na tabela = custo some do /admin/uso-ia
  (ha teste travando).
- **Envs do Railway MANDAM sobre os defaults**: OCR_MODELO_OPUS,
  CADASTRO_IA_MODELO, PLANEJAMENTO_IA_MODELO, GOOGLE_REVIEWS_IA_MODELO,
  TREINO_IA_MODELO, ZAPI_BOT_MODELO — se setadas com modelo antigo, a
  troca de default nao vale; conferir/limpar no painel.

**INCIDENTE do SDK velho (17/08/2026)**: o pin `anthropic==0.40.0`
(11/2024) NAO conhecia o param `thinking` — as 6 chamadas com
`thinking={'type':'disabled'}` da migracao acima estouraram `TypeError`
em TODA execucao por ~2 semanas e NINGUEM notou (best-effort silencioso):
vigia do bot, auditor, follow-up, OCR de NF/boleto, OCR de cupom e
planejamento IA ficaram MORTOS de 05 a 17/08. O Sentry acusava
(GESTAO-PADARIA-3A) mas a cota gratis tinha estourado de ruido. Upgrade
isolado pra `anthropic==0.122.0` com ordem do dono. REGRAS: (1) a suite
mocka a Anthropic — teste verde NAO prova que o SDK aceita um param novo;
ao introduzir param de API, conferir `inspect.signature` contra o pin do
requirements; (2) upgrade de dependencia segue exigindo ordem do dono
(regra de 12/07), este teve.

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
**POLITICA DE RUIDO (17/08/2026, dono: "nao consigo pagar o plano")**: o
plano e o GRATIS e a cota estourou so com transitorios do cron — a
integracao de logging promove todo `logger.error/exception` a evento.
`seru_cron._erro_transitorio` classifica: RuntimeError de SHUTDOWN
(deploy no meio do ciclo) e falha de REDE da Seru pos-retry
(RequestException/_Erro5xx) viram WARNING via `_falha_de_job` (proximo
ciclo re-tenta; Seru fora PERSISTENTE aparece pelos vigias); o resto
segue ERROR → Sentry. O 5xx re-tentavel do `seru._get_uma_vez` loga
WARNING (o ERROR por tentativa gerava evento mesmo quando o retry
resolvia). REGRA: em job de cron best-effort, usar `_falha_de_job` em
vez de `logger.exception` cru; NAO promover transitorio de rede a ERROR.
Testes: `tests/test_seru_cron_ruido.py`.

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

## PDV do TINY — vendas da Cantina entram no sistema (27/07/2026)

A Cantina vende pelo **PDV do Tiny**, nao pelo Seru. Consequencia ate aqui:
as vendas dela eram INVISIVEIS — nao baixavam `EstoqueLoja`, nao entravam em
faturamento nem na previsao de demanda, e a loja nao tinha nenhuma linha de
estoque no sistema. Decisao do dono 27/07: **importar de verdade, como o
Seru** (AskUserQuestion; a outra opcao era so uma sonda de leitura).

**O contrato que torna seguro importar por `pedidos.pesquisa.php`** (conferir
antes de mexer): o NOSSO sistema **so cria NOTA** no Tiny (`tiny_nf.py` →
`incluir_nota_fiscal`); `tiny.incluir_pedido` **nao tem um unico chamador**.
Logo todo `pedido` que a API devolve nasceu no PDV — importar NAO duplica a
baixa que site/B2B ja fazem sozinhos. Se um dia alguem ligar `incluir_pedido`,
esta premissa cai e o sync passa a contar em dobro.

- **Descoberta do payload (sonda `/api/claude/tiny-vendas?de=&ate=`)**: venda
  de PDV vira `pedido` com `nome='Consumidor Final'` e `situacao='Faturado'`.
  `deposito` e `'Geral'` em TUDO — **nao identifica loja**. O que sinaliza a
  Cantina e o sufixo no nome do produto ("SUCO VERDE CANTINA"), mas NEM
  SEMPRE: `CAFÉ EXPRESSO`, `ADICIONAL DE OVOS AO PONTO`, `CHOCOLATE DO PADRE
  GELADO` e `DANISH CALABRESA` vieram sem sufixo. Por isso a loja e
  CONFIGURACAO (`AppConfig.tiny_pdv_loja_id`), nao regra por nome — decisao
  do dono: "so a Cantina" usa esse PDV.
- **Sem loja configurada o sync NAO RODA** (`processar_periodo` devolve
  `erro`): baixar na loja errada e pior que nao baixar.
- **Mapeamento** produto Tiny → receita/produto no `VendaMapa` canal
  `'tiny'` (chave = `nome_externo`; `sku` guarda o `id_produto` do Tiny, que
  e ESTAVEL quando o dono renomeia). Produto sem vinculo vira PENDENTE, e
  PULADO sem alarme e nao trava o resto da venda (espelho do Seru).
  `fator_quantidade` cobre os compostos: "CONE DE PÃO DE QUEIJO COM 5 UN" =
  fator 5. Tela: `/pdv/tiny` (admin) — loja + mapeamento + importar periodo.
- **Baixa pelo motor unico** (`baixa_venda.aplicar_venda`, canal `'tiny'`):
  tipos `venda_tiny` / `venda_tiny_sem_estoque` / `venda_tiny_estorno`, todos
  em `VENDA_TIPOS_LOJA` e os dois primeiros em `VENDA_TIPOS_DEMANDA_LOJA` —
  **a venda da Cantina passa a alimentar a previsao de producao**, que era o
  ponto da integracao. Estorno POSITIVO (familia Seru/lote), sinal -1 na
  demanda.
- **Idempotencia**: `TinyPedidoProcessado` (tabela nova via `db.create_all`,
  sem ALTER). Detalhe indisponivel (falha de rede) => pedido **NAO** marcado,
  retenta no proximo ciclo. Situacao desconhecida (orcamento/aberto) => nao
  baixa e nao marca. Venda faturada e depois CANCELADA no Tiny => estorno no
  ciclo seguinte (`estornar_venda(canal, 'tiny:<id>', 'Tiny #<id>')` — nao
  trocar a ordem: `pedido_ref` e a chave das FRACOES, `referencia` a dos
  INTEIROS; trocar deixa fracao fantasma).
- **Cron**: `seru_cron`, 15 min, janela ontem+hoje, advisory lock 7756,
  kill-switch `TINY_PDV_SYNC=0`.
- **Mapear os ~77 produtos (27/07/2026)**: a tela tem TYPEAHEAD (digita e
  filtra por "contem", acento-insensivel) em vez de `<select>` por linha —
  com 77 linhas x centenas de itens seriam ~38 mil `<option>` e a pagina
  ficaria impraticavel (hoje: 66 KB, zero option na tabela). O catalogo vai
  UMA vez como JSON e o filtro roda no cliente. Digitar INVALIDA a escolha
  anterior (`hidden` zera) — sem isso o texto diria uma coisa e o vinculo
  salvo seria outro. Texto solto sem item escolhido NAO vira vinculo.
- **Sugestao automatica com DOIS pisos** (`sugerir_alvo`/`sugestoes_pendentes`):
  matcher por tokens (tira acento/ruido/'cantina') + `fator_do_nome`
  ("COM 5 UN" -> 5; "300 ml"/"500 g" NAO sao quantidade). `PISO_SUGESTAO`
  =0.5 mostra a dica; **`PISO_PREENCHE`=0.75 e o unico que PRE-PREENCHE**.
  Motivo (validado contra os nomes reais): a faixa 0.50-0.74 produz erro
  CONVINCENTE — "CROISSANT DE AMENDOAS" casava *Creme de Amendoas* e
  "CROISSANT FRANCES" casava *Croissant Almond*, ambos 0.50. Pre-preencher
  isso convida o dono a clicar Salvar num vinculo errado = baixa de estoque
  errada em silencio. Abaixo do piso vira BOTAO "talvez: X — clique pra
  usar". A sugestao NUNCA grava sozinha (`sugestoes_pendentes` e read-only).
- **Aceite em LOTE (dono 01/08/2026: "os que tiverem 100% pode ter um botao
  pra salvar de uma vez")**: `aceitar_sugestoes_lote` grava de uma vez SO os
  matches de score 100% (`PISO_LOTE=1.0` — todos os tokens do alvo no nome
  do Tiny e vice-versa, fora ruido/numeros); recomputa no SERVIDOR (nunca
  confia em lista do navegador) e revalida cada mapa antes de gravar. A
  faixa 75-99% segue exigindo o Salvar individual. Botao na tela com
  confirm + badge verde "100% — entra no aceite em lote" por linha.
- **RE-BAIXA de pedido com ZERO itens baixados** (armadilha do 1o uso real):
  o 1o import da Cantina rodou com NENHUM mapa — todo pedido foi marcado
  processado com 0 baixas, e a idempotencia impedia que o mapeamento
  posterior trouxesse essas vendas. `_processar_pedido` agora RE-baixa
  pedido processado com `n_itens_baixados == 0` quando algum item ganhou
  alvo (nada foi baixado antes -> nao duplica); stats `rebaixados`. Pedido
  todo-ignorado nao entra em loop de refetch; pedido PARCIAL
  (`n_itens_baixados > 0`) NUNCA re-baixa (nao ha idempotencia por item —
  duplicaria o que ja saiu; item mapeado tarde num pedido parcial fica de
  fora mesmo, limitacao aceita). Fluxo canonico pro dono: mapear -> rodar a
  importacao DE NOVO no periodo.
- **Tela sem reload**: o Salvar da linha grava via fetch
  (`X-Requested-With: fetch` -> JSON; sem JS cai no flash+redirect de
  sempre) e o foco pula pro proximo pendente. Teclado: Enter escolhe o 1o
  da lista, Enter de novo salva; setas navegam. Os campos alvo/fator usam o
  atributo `form=` apontando pro form da ultima coluna — `<form>`
  atravessando `<tr>` dependia de quirk do parser HTML.
- **FATURAMENTO da Cantina (01/08/2026, pergunta do dono "e como eu sei o
  faturamento da cantina?")**: ate aqui a resposta era "nao sabe" — a venda
  do Tiny baixava estoque e alimentava a previsao, mas NENHUMA tela mostrava
  o dinheiro (o painel 💰 da home e o /pdv/ leem so o snapshot do Seru, e a
  Cantina nao vende pelo Seru). NAO foi criado snapshot novo: o registro de
  idempotencia `TinyPedidoProcessado` ja guardava `valor` + `data_pedido`;
  `tiny_pdv_sync.faturamento_por_dia/periodo/do_dia_por_loja` so LEEM isso.
  Duas telas: card "Faturamento do PDV do Tiny" no topo de `/pdv/tiny`
  (por dia + total, janela 7/30/90d) e a Cantina somada as outras lojas no
  painel 💰 da home (`briefing_dono._vendas_tiny` entra em `pdv_total`,
  `n_pedidos`, `por_loja` e no delta vs a semana passada; chave nova
  `tiny_total` diz quanto veio de la e a tela/WhatsApp explicitam "inclui
  Tiny" — sem isso o dono compararia com o /pdv/, que e SO Seru, e caçaria
  um erro que nao existe). `cancelados_*`/`desconto` seguem SO do Seru (o
  Tiny nao expoe esses eixos) — os botoes da home dizem isso no title.
  **REGRESSAO fechada junto**: `cancelado_em` so era gravado quando havia
  estoque baixado; com o faturamento lendo esta tabela, venda cancelada SEM
  baixa (produto nao mapeado — o estado do 1o import) contaria como dinheiro
  PRA SEMPRE. Agora o marcador e gravado sempre e o ESTORNO segue condicional
  a ter havido baixa (stat novo `cancelados`). **Dia que nao aparece e dia
  NAO IMPORTADO, nao dia sem venda** (o cron cobre so ontem+hoje) — as duas
  telas dizem isso; por isso tambem a Cantina fica "sem comparacao" (delta
  None, nunca -100%) enquanto nao houver a semana passada importada.
  `faturamento_do_dia_por_loja` agrupa pela loja GRAVADA na venda, nao pela
  config atual: trocar `tiny_pdv_loja_id` nao reatribui faturamento passado.
  Sonda `/api/claude/tiny-vendas` ganhou o bloco `importado` (responde ANTES
  do gate do token do Tiny — diagnostica com a API do Tiny fora).
- Testes: `tests/test_tiny_pdv_sync.py` (31 casos, Tiny sempre mockado) +
  secao "PDV do Tiny no cockpit" em `tests/test_briefing_dono.py` (7 casos).

## Dias de funcionamento da loja (27/07/2026)

Pedido do dono: "Cantina nao precisa lancar sobras durante a semana pois so
funciona de sabado e domingo". Antes, a cobranca de sobras listava TODA loja
ativa em TODO dia — a Cantina levava **5 lembretes por dia util** (Slack
20:10/15/20/25 + WhatsApp do dono as 20:30) por sobra que nao existia.

- **Coluna** `Loja.dias_funcionamento` VARCHAR(7) — os dias em que a loja
  ABRE, em digitos do `date.weekday()` (0=segunda ... 6=domingo): `'56'` =
  sabado e domingo. Procedimento de 2 commits (ALTER em `migrations_legacy`
  PG+SQLite deployado e confirmado pela sonda `/api/claude/deploy` ANTES do
  modelo). Backfill UNICO na criacao da coluna marca `'56'` em quem casa
  `LOWER(nome) LIKE '%cantina%'` — nunca sobrescreve edicao futura do dono.
- **VAZIO/NULL = abre TODO DIA**, que e o valor de todas as lojas antigas —
  a feature nasce sem mudar comportamento de ninguem. Isso e **fail-open
  DELIBERADO**: loja mal configurada continua sendo cobrada; sumir da
  cobranca em silencio por causa de config faltando seria o erro caro.
- **Consumidor unico hoje**: `desperdicio_alerta.lojas_sem_desperdicio(dia)`
  filtra por `Loja.funciona_em(dia)`. Conferido que e o UNICO ponto que cobra
  lancamento de sobras — o "Precisa de voce hoje" e o `alertas_operacionais`
  so tratam de *retirada de sobra presa em transporte*, coisa diferente.
- **Onde se edita**: checkboxes dos 7 dias no card da loja em `/rh/lojas`
  (mesmo form dos dados fiscais; o botao virou "Salvar dados da loja"). O
  POST usa `getlist` + whitelist `0123456` — valor forjado nao entra na
  coluna. Nenhum dia marcado grava NULL (volta a "abre todo dia").
- **NAO** usar a flag pra capar previsao/pedido/estoque sem ordem do dono: o
  pedido foi so sobre a COBRANCA de sobras. Uma loja fechada segunda ainda
  pode ter estoque e pedido pendentes.
- Testes: `tests/test_loja_dias_funcionamento.py` (11 casos, incluindo a
  regressao do fail-open e o POST forjado). Manual de operacao atualizado.

## Cobranca de sobra POR ITEM (01/08/2026) — caso croissant tradicional

Caso real ("na conferencia de estoque das lojas tem dado uma diferenca
enorme" -> "o pessoal nao tem lancado sobra do croissant tradicional,
precisamos atacar isso"). DIAGNOSTICO pelas sondas novas: os ajustes de
conferencia do dono (29-31/07) mostraram DOIS padroes — (a) itens que ele
NAO controla (Pao de Queijo MP, sistema 0 vs prateleira 1.221; a venda
drena sem nunca ter entrada — nao ajustar, sao esperados ate ele
controlar) e (b) paes/viennoiserie com sistema ~2x a prateleira: o razao
(`/api/claude/estoque-ledger`) provou SOBRA NAO LANCADA — Pao Frances na
Ribeiro com 1.050 recebidos, 558 vendidos e ZERO desperdicio em 14 dias
(rombo ~492). CAUSA SISTEMICA: o alerta das 20h
(`desperdicio_alerta.lojas_sem_desperdicio`) cobrava so a LOJA ("lancou
ALGO hoje?") — lancar a sobra de UM item calava a cobranca de todos.

- **Flag** `Receita.cobra_sobra_diaria` (checkbox na ficha, junto do
  reaproveitavel; procedimento de 2 commits com sonda /api/claude/deploy).
  Seed UNICO na criacao da coluna (`migrations_legacy.COBRA_SOBRA_SEED`):
  os 16 itens que o dono AJUSTOU na conferencia (pao frances, croissant,
  sourdoughs, danishes, cinnamon, cookie, brioche, almond). Depois disso a
  ficha manda. `duplicar` copia a flag; sonda `/api/claude/receita` expoe.
- **`desperdicio_alerta.itens_sem_sobra(dia)`**: receita flagged +
  saldo > 0 no EstoqueLoja da loja + NENHUM `Desperdicio` da receita
  naquela loja no dia => cobranca NOMINAL na mensagem ("Croissant
  Tradicional (45) sem sobra lancada — lance a sobra ou confira o
  estoque"). Mesma regua de loja do alerta por-loja (operacional +
  `funciona_em`); arquivada fora; cap `_MAX_ITENS_POR_LOJA=8` + "e mais
  N". O SALDO vai na mensagem de proposito: se a loja vendeu tudo e o item
  aparece, e divergencia de estoque — o gesto e conferir, nao ignorar.
- Os senders (Slack 20:10-25 + WhatsApp dono 20:30) disparam se HOUVER
  QUALQUER pendencia (loja OU item) — antes, loja que lancava 1 item sumia
  e levava os itens junto. A cobranca por-loja continua existindo.
- **WhatsApp do dono e RESUMO desde 14/08/2026** (dono: "muita informacao,
  so fala se lancaram ou nao — esta ficando flodado"): `mensagem_resumo` —
  uma linha por loja ("nao lancou nada" / "lancou parcial, N itens sem
  sobra"), SEM nome de item nem saldo, com o aviso "detalhe por item no
  Slack". A lista NOMINAL (`mensagem_pendentes`) continua no Slack, que e
  onde o gerente age. NAO voltar a lista nominal pro WhatsApp sem ordem.
- **LIMITACOES CONHECIDAS (pos-revisao, aceitas)**: (1) lancamento PARCIAL
  nao e detectavel (lancou 5 croissants mas sobraram 50 — a linha de
  Desperdicio existe, o item some da cobranca; so a contagem fisica pega).
  (2) Desperdicio lancado num PRODUTO homonimo (`receita_id=None`) NAO cala
  a receita flagged — o bot fuzzy pode resolver "pao frances" pro Produto
  'Pao Frances' e a receita 'Pão Francês Fermentado' seguir cobrada; mapear
  produto->receita no set de lancados e decisao separada do dono (risco:
  fadiga de alerta). (3) Loja sem NENHUM lancamento aparece na lista
  por-loja E na secao de itens — DELIBERADO (mais informacao, nao
  duplicata). (4) A guarda `_itens_sem_sobra_safe` nos senders e
  best-effort COM exception no log: a cobranca por-loja pre-existente
  nunca morre por falha da query nova. (5) A tela /admin/slack-diagnostico
  conta so lojas (cosmetico); o retorno dos senders ja expoe
  `pendentes_itens`.
- **Sondas criadas no diagnostico** (read-only): `/api/claude/
  conferencia-loja` (os ajustes de conferencia do dono por item/loja, com
  sinal: + = sistema tinha MENOS que a prateleira) e `/api/claude/
  estoque-ledger` (razao de MovEstoqueLoja por TIPO de um item numa loja).
  ATENCAO ledger: o canal Seru/lote grava BAIXA como quantidade POSITIVA
  (`baixa_venda._SINAL_ESTORNO` — so o site grava negativo), entao NUNCA
  somar entrou/saiu pelo sinal; ler por tipo.
- Testes: `tests/test_sobra_por_item.py` (15 casos). Manual atualizado
  (linha das sobras no DIARIO).

## Aviso de pedido recebido = DIGEST 12:00 (14/08/2026)

Pedido do dono ("os pedidos recebidos pelas lojas podem ser acumulados ate
as 12:00 dai dispara uma unica mensagem ao inves de mandar picado — esta
ficando flodado e eu nao estou vendo as mensagens"): o WhatsApp por pedido
na hora da entrega FOI DESLIGADO (call sites removidos em
`pedidos/routes.py` receber e `copilot.executar_mudar_status_pedido`; ha
teste travando contra reintroducao). Em vez disso:

- **`pedidos_notificacao.enviar_digest_recebimentos()`** roda as 12:00 BRT
  (`seru_cron`, job `zapi-digest-recebimentos`, lock **7760**, mesmo
  kill-switch `ZAPI_BOT_AVISO_RECEBIMENTO=0`): UMA mensagem com TODOS os
  pedidos entregues ainda sem aviso — um bloco por pedido (loja, contagem
  de fotos, link da pasta de conferencia no Dropbox).
- **Idempotencia** = o MESMO sentinela `[avisado-fotos]` em
  `pedido.observacao` de sempre; o digest acha pendentes por
  `status='entregue'` + ausencia do sentinela, janela `_JANELA_DIAS=3`
  por `data_entrega` (fallback `criado_em` se NULL). Envio falho nao
  marca ninguem (re-entra no digest seguinte); recebido APOS as 12:00
  entra no digest do dia seguinte (aceito pelo desenho — entrega e de
  manha).
- `notificar_pedido_recebido` (aviso imediato de UM pedido) segue vivo SO
  pra rota de teste do owner `/admin/teste-aviso-recebimento` (valida o
  pipe Z-API+Dropbox). O corpo por pedido e compartilhado
  (`_linhas_pedido`/`_fotos_e_link`).
- **POS-REVISAO (fixados 14/08)**: (1) `_executar_recebimento_pedido`
  agora CARIMBA `pedido.modificado_em` no ato do recebimento (web, QR e
  copilot passam por ele) e a janela do digest olha esse carimbo tambem —
  sem isso, pedido recebido com ATRASO (>3d da `data_entrega`, que e a
  data PLANEJADA) saia da janela e nunca era avisado; `modificado_por_id`
  fica intocado de proposito (semantica de protecao dos auto-pedidos).
  (2) o digest PULA pedido com `[PEDIDO-TESTE-AVISO]` na observacao (o
  sintetico da rota de teste nao vaza pro digest real se o envio imediato
  do teste falhar). (3) cap `_MAX_PEDIDOS_DIGEST=20` + "e mais N no
  proximo digest" (excedente nao e marcado). (4) commit das sentinelas
  falhando apos envio OK devolve `marcados: False` (digest de amanha
  repete — duplicar > perder, mas o retorno diz). (5) fallback de link
  usa `APP_BASE_URL` (relativo nao clica no WhatsApp). ACEITO (era assim
  no aviso antigo): pedido estornado e RE-recebido nao re-avisa (sentinela
  fica na observacao).
- Testes: secao "Digest das 12:00" em
  `tests/test_pedido_aviso_recebimento.py`. Manual atualizado (RODA
  SOZINHO).

## Perdas de PRODUCAO na tela do padeiro (13/08/2026)

Pedido do dono ("colocar as perdas na tela do padeiro, eles precisam ter
uma aba para lancar se queimou algo"). Decisoes dele via AskUserQuestion:
perda de item PRONTO **debita EstoqueProducao** (saturando em 0); opcao
**"fornada queimada"** consome MP + sub-receitas prontas da ficha SEM
creditar (o produto nunca existiu); relatorio admin com **custo em R$**
pela ficha.

- **Modelo** `PerdaProducao` (app/models/estoque.py, tabela nova via
  db.create_all; em AUDITED_MODELS). **Service**
  `app/services/perda_producao.py`: `registrar` (validacoes com ValueError
  legivel; teto QTD_MAXIMA=2000 contra dedo errado; guarda de duplo
  lancamento — mesma receita+qtd+usuario em <30s recusa, padrao checklist;
  fornada de receita ARQUIVADA recusa — item pronto de arquivada segue
  escoavel), `excluir` (admin; CLAIM ATOMICO por DELETE condicional —
  2 admins concorrentes nao creditam 2x; estorno EXATO pelos movs
  'Perda #<id> — ' com delimitador anti #1×#11; fornada NAO tem estorno
  automatico — MP/ConsumoSubFracao irreversiveis, recusa orienta o acerto
  manual de MP e /pedidos/congelados) e `listar` (custo 1x fora do loop
  por NOME, joinedload, cap 500 com flag `truncado` visivel).
- **Ledger**: tipos `perda_producao` (debito), `perda_producao_sem_estoque`
  (neutro pelo sufixo) e `perda_producao_estorno` (credito —
  MOV_PRODUCAO_CREDITOS). Labels em `historico_humano.TIPOS_MOV_PRODUCAO`
  + dropdown do /pedidos/congelados/historico. `saida_producao`
  (estoque_congelados) ganhou kwarg `tipo=` (default intacto).
- **Fornada queimada**: `producao.consumir_ficha(rec, unidades, user_id,
  referencia_mp)` — EXTRAIDO do bloco inline de `produzir_item_plano`
  (MP por consolidar_lista_compras com mult fracionario + subs por
  consumir_subreceitas_prontas; devolve a lista das subs pra tela avisar
  falta de congelado). Pre-baixa de MP e plano do dia INTOCADOS de
  proposito (a falta segue reservada pro re-assamento; re-assar = 2x MP
  fisica, correto). MP do mov leva 'Fornada queimada ... — perda #N'.
- **Telas**: `/padeiro/perdas` (padeiro_required; link 🔥 no header da TV;
  standalone dark, validada a 390px com Playwright; typeahead reusa
  buscar-receitas.json filtrando receita — ref forjada nao-receita recusada
  no server; motivos como botoes, 'outro' exige observacao) e
  `/producao/perdas` (admin; periodo 7/30/90d, custo unit×qtd + total,
  excluir com confirm; link na area Producao). Manual atualizado (DIARIO +
  resumo por papel) na mesma mudanca.
- **ACEITOS (revisao, baixa severidade)**: mov de sub-receita da fornada
  sai como 'Consumo p/ <pai>' sem vinculo com a perda (referencia e
  compartilhada com o produzir); movs orfaos apos exclusao (trilha
  preservada, padrao desperdicio); custo por NOME herda a fraqueza das
  homonimas do custos.py; `consumir_ficha` grava o mov de MP com a
  quantidade CHEIA mesmo saturando o saldo em 0 (herdado byte-a-byte do
  produzir — pre-existente).
- **RESPONSÁVEL pela perda (follow-up do dono, mesmo dia)**: select
  obrigatório com o QUADRO DO RH (`PerdaProducao.funcionario_id`, ALTER
  pelo procedimento de 2 commits com sonda ?colunas= — a conta da TV é
  compartilhada, criado_por só diz quem digitou).
  `perda_producao.responsaveis_producao()` filtra funcionários ativos por
  FUNÇÃO normalizada (`FUNCOES_RESPONSAVEL` = padeir/produc/confeit/
  forneir/massa — cobre padeiro, ajudante de padeiro, auxiliar de
  produção); NINGUÉM casando = fallback pra todos os ativos (fail-open —
  RH renomeado nunca trava a perda). `registrar` exige funcionário ATIVO;
  relatório mostra Responsável + "lançado por" quando diferem.
- Testes: `tests/test_perda_producao.py` (24 casos).

## Checklist de loja — abertura / troca de turno / fechamento (03/08/2026)

Pedido do dono: o gerente/atendente chefe responsavel do turno preenche um
checklist e tira FOTO comprovando os pontos necessarios. Decisoes dele
(AskUserQuestion): tela no CELULAR (nao Slack), itens CADASTRAVEIS em tela
(sem deploy), foto POR ITEM selecionado, cobranca = pendencia na home.

- **Modelos** (`app/models/checklist.py`, tabelas novas via `db.create_all`):
  `ChecklistItemModelo` (tipo/texto/exige_foto/ordem/ativo/loja_id NULL =
  todas), `ChecklistPreenchimento` (loja/tipo/data/usuario; SEM unique — troca
  de turno pode repetir no dia) e `ChecklistResposta` com **SNAPSHOT**
  (`item_texto`/`exigia_foto`): editar o cadastro depois nao reescreve a
  historia. Foto so Dropbox (`foto_url` raw, regra M6), path
  `/checklists/<loja>/<data>/<item>_<ms>.jpg`.
- **Service** `app/services/checklist_loja.py`, tudo FAIL-CLOSE (nada e
  gravado em erro; uploads ANTES do 1o INSERT): todo item respondido;
  `exige_foto` sem foto = recusa (vale tambem em "problema" — a foto prova o
  estado); problema sem observacao = recusa; Dropbox fora/imagem ilegivel/
  erro de REDE do upload = recusa com mensagem legivel (catch largo
  deliberado — ConnectionError do retry escapando viraria 500 e o
  funcionario perderia as marcacoes). **Fechamento de madrugada** (antes de
  `HORA_VIRADA_FECHAMENTO`=04:00) grava `data` = dia ANTERIOR (turno de
  segunda fechado 00:15 de terca e fechamento de SEGUNDA — mesma classe do
  padeiro pos-meia-noite).
- **Telas** (blueprint `checklist`, prefixo `/checklist`): hub + preencher
  (mobile 560px, radios OK/Problema, input de foto SEM `capture=` — armadilha
  iOS 24/06/2026; re-render de erro preserva marcacoes e avisa que fotos
  precisam re-anexar; anti-duplo-submit = botao desabilita no JS + guarda de
  30s no servidor por loja+tipo+usuario), `/checklist/config` (admin; item
  usado nunca e excluido, so desativado; loja invalida no cadastro NAO vira
  item global em silencio — recusa) e `/checklist/conferencia` (admin;
  eager-load de loja/usuario/respostas; topo mostra quem esta DEVENDO
  hoje/ontem). Validado a 390px (Playwright, 16 checks).
- **Permissao**: capacidade editavel `web_checklist` (default gerente+
  funcionario; admin/owner sempre) — decorator `checklist_required` +
  `Usuario.pode_checklist()`. O atendente chefe (papel funcionario) NAO ve a
  area Lojas: a sidebar tem bloco avulso "Loja → Checklist da loja" pra quem
  tem a capacidade sem `pode_lojas` (`base.html`). `ChecklistItemModelo` em
  `AUDITED_MODELS` (editar item muda o que os turnos comprovam).
- **Cobranca** (`pendencias_checklist` → `briefing_dono.pendencias`, com
  try/except que nunca derruba a home): abertura de HOJE ausente so depois de
  `HORA_COBRA_ABERTURA`=10:00; fechamento de ONTEM ausente. Respeita
  `funciona_em` (Cantina so sab/dom), so cobra loja com item aplicavel, item
  criado DEPOIS do dia cobrado nao conta (cadastrar o 1o item de fechamento
  hoje nao acusa "ontem" retroativo), e SEM NENHUM item cadastrado a funcao
  curto-circuita num EXISTS (feature nao configurada nao custa nem cobra).
  Troca de turno NUNCA e cobrada.
- **DECISOES ACEITAS** (mencionadas ao dono): funcionario pode preencher
  checklist de QUALQUER loja (gerente cobre outra loja; o registro guarda
  quem — travar por `Usuario.loja_id` e decisao separada); hora de cobranca
  10:00 e GLOBAL, nao por loja; foto extra em item sem exige_foto e aceita.
- **IMPORTACAO do checklist em papel (03/08/2026)**: o dono mandou o PDF
  "CHECKLISTS OPERACIONAIS POR SETOR" (11 folhas — Cafe/Barista, Chapa,
  Cozinha, Viagem/Embalagem, Camara Fria, Caixa, Salao, Limpeza, Area
  Externa, Escritorio e Forno, Supervisao da Loja) e pediu pra importar.
  Escolhas dele (AskUserQuestion): **tudo numa tela agrupado por setor**
  (nao navegacao por setor), **"Durante o expediente" virou TIPO PROPRIO**
  (`CHECKLIST_TIPOS` ganhou `'durante'`; `troca_turno` segue separado) e
  **nenhum ponto entra exigindo foto** ("os check que EU selecionar").
  - Coluna `ChecklistItemModelo.setor` VARCHAR(60) — e SUBTITULO na tela,
    NULL = grupo "Geral". Sem ela o agrupamento viraria prefixo no texto
    ("CAFE — Ligar maquina…"), que sujaria tambem o snapshot historico.
  - Seed em `app/services/checklist_seed.py` (169 pontos; a linha
    "RESPONSAVEL" da Supervisao ficou de fora — o sistema ja grava quem
    preencheu). Blocos sem tipo proprio: "MANHA" (Limpeza) → abertura;
    "PADRAO DO CAFE", "ORGANIZACAO PEPS", "MEIO DO DIA" → durante.
    Distribuicao: 60 abertura / 64 durante / 45 fechamento.
  - `importar_padrao()` roda UMA vez (guard AppConfig `checklist_seed_
    opao_v1`, chamado de `migrations_legacy._seed_checklist_padrao`,
    pulado sob PYTEST_RUNNING) e **nunca ressuscita** — apagar/editar item
    depois manda sobre o seed. `forcar=True` ignora so o guard: a dedup por
    (tipo, setor, texto) impede duplicata em qualquer caso.
  - `checklist_loja.agrupar_por_setor` preserva a ordem de PRIMEIRA
    aparicao (item novo com ordem 0 no meio nao faz o setor sair repetido).
  - ARMADILHA REAL desta mudanca: o auto-commit hook pusha a CADA edicao,
    entao cada push CANCELA o CI anterior — com Wait-for-CI, o deploy do
    "commit 1" (ALTER) nunca subiu sozinho e ALTER+modelo cairam no MESMO
    deploy. Foi seguro AQUI porque `_setup_schema` roda `db.create_all()` →
    `_migrate()` ANTES de servir request, entao a coluna nasce antes do
    primeiro SELECT. Pra valer o procedimento de 2 commits DE VERDADE, o
    commit 1 tem que ser pushado e o deploy CONFIRMADO pela sonda antes de
    tocar qualquer outro arquivo (o hook nao espera).
- Testes: `tests/test_checklist_loja.py` (49 casos). Manual de operacao
  atualizado (secao DIARIO) na mesma mudanca.

## Uptime Kuma — vigia EXTERNO no VPS (04/08/2026)

Pedido do dono depois de perguntar que ferramentas open source ajudariam.
Motivo tecnico: TODOS os vigias (`seru_cron`) e o Sentry rodam DENTRO do
app — cobrem "app de pe e algo deu errado" e ficam MUDOS em "app nao sobe /
crashloop / banco fora / deploy travado". Nesse caso o silencio e
indistinguivel de "tudo bem". O Uptime Kuma roda FORA e fecha essa classe.

- **Arquivos**: pasta `uptime_kuma/` (espelha o padrao do `wifi_radius/`):
  `docker-compose.yml`, `docker-compose.https.yml` (overlay com Caddy +
  Let's Encrypt), `Caddyfile`, `setup.sh` (idempotente) e `README.md` com
  os monitores e a notificacao.
- **Onde roda**: VPS Vultr SP que ja existe pra ponte RADIUS do Wi-Fi.
  NUNCA no Railway (cairia junto com o alvo). O `setup.sh` NAO mexe em
  firewall DE PROPOSITO — ligar UFW nesse VPS sem `ufw allow 1812/udp`
  derruba a autenticacao do Wi-Fi das lojas.
- **Monitores**: `/health` do gestao (Keyword `ok` — status 200 do proxy
  com pagina de erro nao basta), `opao.online`, Chatwoot, e porta TCP 1812
  (a propria ponte RADIUS). Retries=2 pra nao alarmar por blip de rede.
- **ARMADILHA REAL (testada com liquidjs)**: o exemplo de Custom Body que a
  tela do proprio Uptime Kuma sugere usa `"{{ msg }}"` ENTRE ASPAS. Isso
  QUEBRA — o corpo e renderizado por LiquidJS e precisa sair JSON valido, e
  msg de queda costuma ter aspas/quebra de linha (`getaddrinfo ENOTFOUND
  "gestao"`). Resultado: o alerta nao sai justamente na hora da queda. O
  certo e `{{ msg | json }}` SEM aspas em volta (o filtro ja produz a
  string escapada). Validado nos 4 casos: aspas, multilinha, barra
  invertida e mensagem de recuperacao.
- **ARMADILHA 2, pior que a primeira (custou a sessao inteira de
  04/08/2026)**: no branch `custom` o `webhook.js` do Kuma renderiza o
  template pra uma STRING e **NAO define Content-Type**; o axios, com
  string e sem header, manda `application/x-www-form-urlencoded`. A Z-API
  le como formulario, nao acha `phone` e responde `400 {"error":"Phone is
  empty"}` — COM o JSON perfeito no corpo. O sintoma aponta pro lugar
  errado (parece dado faltando). Fix: por `"Content-Type":
  "application/json"` nos **Additional Headers**, que o Kuma mescla DEPOIS
  do branch. Comprovado com axios real contra servidor de eco.
- **Webhook fala com a Z-API DIRETO** (`/send-text` + header
  `Client-Token`), sem passar por `app/services/zapi.py` — logo fora do
  whitelist e do teto/hora. E o desejado: alerta de queda nunca pode ser
  suprimido por throttle.
- **INSTALADO em 04/08/2026** pelo dono (nao ha SSH do container de dev — a
  saida so libera HTTPS; ele rodou no terminal dele). Estado: `/opt/
  uptime-kuma`, HTTP na 3001, Docker 29.7.1 / Ubuntu 26.04, container
  `unless-stopped`. Confirmado no ato: `wifi-radius.service` esta
  **enabled** (sobe no boot -> reiniciar o VPS e seguro pro Wi-Fi) e
  instalar o Docker nao derrubou a ponte.
- Pendencias opcionais: HTTPS (DNS `status.opaopadariaartesanal.com.br` +
  `./setup.sh <dominio>` — hoje a senha trafega em claro na 3001) e um
  UptimeRobot free vigiando o proprio Kuma.
- Registrado no manual de operacao (secao RODA SOZINHO).

## E-mail marketing (Listmonk) — base + aniversario (05/08/2026)

Pedido do dono: "preciso de um opensource para disparar propaganda, feliz
aniversario, etc para os e-mails cadastrados no banco de dados. Temos tanto
dos clientes que usam o Wi-Fi da loja quanto os que compraram no site" +
"usarei os dados de quem usa wi-fi para marketing" + planilha de um sorteio
como terceira base. Escolha: **Listmonk** (Go + Postgres, self-hosted).

- **Onde roda**: VPS Vultr SP (o mesmo do Wi-Fi RADIUS e do Uptime Kuma),
  `https://mkt.opaopadariaartesanal.com.br` com Let's Encrypt; a 9000 fica
  FECHADA pra internet. Envio pelo **stream BROADCAST do Postmark**
  (`smtp-broadcasts.postmarkapp.com:587`), separado do transacional DE
  PROPOSITO: reclamacao de spam numa campanha nao pode derrubar a entrega
  de e-mail de pedido/magic link. Remetente `pedidos@opao.online`.
- **Envs (Railway)**: `LISTMONK_URL`, `LISTMONK_API_USER` (default
  `api_padaria`), `LISTMONK_API_TOKEN`. Sem elas o modulo fica DORMENTE
  (`listmonk.disponivel()` False) e nada quebra. `_req` RECUSA URL que nao
  seja `https://` — o token vai em BasicAuth.
- **REGIME = OPT-OUT, decisao do dono (registrada em `marketing.py`)**: a
  base inteira entra e quem clicar em "cancelar inscricao" para de receber.
  Levantei que o aceite do portal Wi-Fi foi dado pra *usar o Wi-Fi* (base
  mais fragil que a de quem comprou); ele reafirmou que quer as duas.
  Salvaguardas: link de descadastro em todo e-mail + `Cliente.
  marketing_descadastro_em` (ALTER pelo procedimento de 2 commits).
- **Tres listas por ORIGEM** (`Clientes do site` = tem PedidoOnline com
  `pago_em`, divulgacao fora; `Wi-Fi das lojas`; `Sorteio 2026`, importado
  por planilha e que NAO sincroniza) + a TRANSIENTE `Aniversariantes de
  hoje`.
- **`Cliente.origem`** ('site' | 'wifi' | 'balcao' | NULL) — coluna nova
  (procedimento de 2 commits; ALTER `2768944d` confirmado pela sonda
  `?colunas=cliente.origem` antes do modelo). BUG REAL que a criou (dono
  05/08: "So tem 1 cliente do wi-fi?"): `contatos_do_wifi` derivava a
  origem de `WifiPortalSessao`, que so existe no fluxo ANTIGO (validacao
  por WhatsApp) — o caminho VIVO desde 13/07 e
  `wifi_portal.criar_conta_direta` (modo RADIUS), que cria SO o `Cliente`.
  A lista mostrava 1 pessoa. Agora a origem e gravada NA HORA em
  `criar_conta_direta`, no `_resolver_conta` do portal, no cadastro do site
  e no checkout de convidado; a sessao fica so como rede de seguranca.
  Backfill unico na criacao da coluna: `aniversario_dia IS NOT NULL` OU tem
  sessao => 'wifi' (os dois formularios do portal sao os UNICOS lugares do
  sistema que perguntam aniversario — conferido; o cadastro do site nao
  pergunta). REGRA: cadastro NOVO de cliente grava `origem` — nao inferir
  depois.
- **Import de planilha na tela** (`marketing.contatos_de_planilha` +
  `importar_planilha`, rota `/admin/marketing/importar`): xlsx ou csv, acha
  as colunas pelo NOME no cabecalho (e-mail/nome/sobrenome/telefone — a
  ordem muda entre formularios) e descarta invalido/repetido/sem e-mail com
  contagem visivel. Foi assim que a base do sorteio entrou (643 validos de
  677 linhas; 34 repetidos).
- **Sonda `/api/claude/catalogo-site?busca=`** (read-only): nome, preco,
  FOTO e link de cada item publicado — o assistente precisa disso pra
  escrever campanha, e as paginas da loja so respondem no host da loja
  (opao.online), que o proxy do container nem alcanca. O `url` sai de
  `href` do proprio catalogo (mesma fonte do sitemap).
- **Por que a lista transiente**: campanha do Listmonk mira LISTA, nao
  consulta (conferido na doc da API — o POST /api/campaigns nao aceita
  segmentacao SQL). Entao a campanha do dia esvazia e reconstroi essa lista
  com `PUT /api/subscribers/query/lists` consultando
  `subscribers.attribs->>'aniv_dia'` — por isso dia/mes viajam em `attribs`
  no import.
- **ARMADILHA que isso cria e que esta fechada**: quem clica em "cancelar"
  no e-mail de aniversario cancela **na lista transiente**, que e apagada no
  dia seguinte — o "nao quero mais" sumiria. Por isso `campanha_aniversario`
  **colhe os descadastros ANTES de reconstruir** e `marcar_descadastros`
  **propaga o unsubscribe pra TODAS as listas** (por ID, via
  `listmonk.mudar_listas` — nunca montando SQL com o e-mail) alem de marcar
  no banco. NUNCA inverter essa ordem nem tirar a propagacao.
- **Disparo automatico NASCE DESLIGADO** (`AppConfig marketing_aniv_ativo`):
  o primeiro e-mail de marketing pra base real e gesto do dono na tela, nao
  efeito colateral de deploy. Desligado, o cron so deixa a campanha em
  RASCUNHO no Listmonk.
- **Teto de sanidade** `MARKETING_ANIV_TETO` (default 200): mais gente que
  isso fazendo aniversario no MESMO dia = consulta errada, nao festa — nao
  envia e loga erro. Idempotencia do dia em `AppConfig marketing_aniv_ultimo`
  (nao manda dois "parabens" pra mesma pessoa se o job reexecutar).
- **Cron**: `seru_cron` 09:00 BRT, advisory lock **7750** (reciclado do
  `briefing-dono`, removido em 17/07/2026), kill-switch `MARKETING_AUTO=0`.
  Sincroniza a base e monta a campanha do dia, nessa ordem.
- **Enviar teste** (`marketing.enviar_teste`, rota `/admin/marketing/teste`):
  cria campanha em RASCUNHO mirando a lista `Testes internos` e usa o
  `POST /api/campaigns/<id>/test` — NUNCA `iniciar_campanha` (ha teste
  travando). Cadastra o destinatario antes (`garantir_assinante`, 409
  tolerado): o Listmonk so testa pra quem JA e assinante, senao o botao
  falharia na primeira vez. `content_type='html'` (o dono cola HTML pronto).
- **Tela** `/admin/marketing` (owner; link na area Administracao): listas com
  contagem, "Sincronizar agora", editor do assunto/corpo do aniversario
  (`{{ .Subscriber.FirstName }}` e template do Listmonk, nao Jinja), a chave
  do automatico, "Criar rascunho de hoje" e "Enviar agora". As PROMOCOES o
  dono escreve e dispara dentro do proprio Listmonk.
- **REGRA: todo link de campanha leva UTM.** Sem UTM o GA4 joga o clique em
  "direto" e o trafego do e-mail fica INDISTINGUIVEL de quem digitou o
  endereco — nao da pra separar depois, o dado nasce perdido. Padrao:
  `?utm_source=listmonk&utm_medium=email&utm_campaign=<slug-da-peca>`
  (ex. `dia-dos-pais`); se houver mais de um link pro mesmo produto,
  `utm_content=<posicao>`. Vale pro botao E pra foto clicavel — as duas sao
  link. Caso que criou a regra (05/08/2026, 1a campanha real): Dia dos Pais,
  1.038 enviados / 305 aberturas / 20 bounces (1,9%), 27 pedidos criados e
  R$ 7.886,50 no dia com 20 clientes NOVOS — e nenhuma forma de provar no
  GA4 quanto veio do e-mail. O Listmonk conta clique por link (stats da
  campanha), mas so ATE o clique; o que acontece dentro do site so o UTM
  amarra.
- Testes: `tests/test_marketing.py` (21) + `tests/test_listmonk.py` (12) —
  `requests`/Listmonk SEMPRE mockados, nenhum teste dispara e-mail. Manual
  de operacao registrado (RODA SOZINHO + QUANDO PRECISAR).

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

**Retorno tambem nunca ganha PREVISTO/producao no motor de VENDAS (fix
13/07/2026)**: a venda do Nutella baixa o retorno DA LOJA e a coleta de
retirada gera movimento — a receita-retorno tinha historico de venda
PROPRIO e no motor=vendas virava "previsto 164 → produzir 164" no grid
(caso real; 45 un chegaram numa ordem enviada). Guardas em camadas:
balanco zera previsto+produzir de retorno_ids (`balanco_industria`),
`editar_celula` recusa (`receita_retorno`), `aplicar_overrides` ignora
override legado, `_sync_itens_do_cronograma` pula a linha (re-enviar a
ordem REMOVE item de retorno sem producao) e a tela trava a celula com a
tag "♻️ retorno — nao se produz". Testes: secao "Retorno nunca produzivel"
em `tests/test_cronograma_ux.py`.

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

**Flag "estoque nao abate" + ficha do croissant a 86 g (19/07/2026)**: caso
real — a linha da Massa para folhar mostrava "em estoque: 2" (ledger, nao
geladeira) e a sugestao de massa pros 300 pains de terca saia 5 em vez de 7
("nao e so isso de massa que eu preciso... nao tenho 2 massas la e mesmo se
tivesse nao deveria considerar"). Decisoes do dono, todas na mesma conversa:
- `Receita.estoque_nao_abate` (checkbox na ficha; ALTER + backfill unico
  marcando a Massa para folhar em `migrations_legacy`, procedimento de 2
  commits): o estoque FISICO da receita nunca abate a producao sugerida —
  balanco (`produzir = alvo - wip` em vez de `- est_efetivo`), MRP
  (`_explodir_bom._estoque_livre` usa so `em_producao`) e o shaping por dia.
  A producao JA MANDADA (WIP do plano de hoje) SEGUE contando (senao a
  sugestao de amanha duplicaria a ordem em execucao) e cobre a vespera;
  fisico ignorado que nao cobre vespera vira aviso `insumo_sem_vespera`
  (desejado: o dono confere a geladeira na mao). Pos-revisao: na LINHA do
  cronograma, saldo/produzir/projecao/entregas_risco tambem usam o numero
  de planejamento (WIP) — fisico fantasma nao faz a caixa dizer "nao
  falta" com a linha produzindo (classe do bug de 30/06) nem CALA o 🚨 de
  entrega em risco; o real segue visivel em `em_estoque`. Tag "📦 estoque
  nao abate" na linha do grid + campo nas sondas de receita e cronograma.
  O consumo REAL na producao continua debitando EstoqueProducao — flag e
  SO de planejamento. Consequencia ACEITA: a offset 0 (grid comecando
  hoje) o WIP e vazio por desenho, entao a massa flagged mostra o aviso
  "sem vespera" com frequencia — e o convite pra conferir a geladeira,
  nao um bug.
- Ficha do croissant tradicional: 86 g de massa/un = 50 x 86 / 3.580 =
  **1,2011 bola/batida** (estava 1.0 = 71,6 g; o 1,257 de 03/07 era 90 g).
  Backfill guardado em `porcentagem = 1.0` no mesmo commit 1.
- Pain au Chocolat confirmado 1,0 bola/lote de 45 (~80 g/un) — nao mexer.
Testes: `tests/test_estoque_nao_abate.py` (7 casos).

**Sub-receita DE AMASSADEIRA vs DE MONTAGEM (15/07/2026)**: o dono converteu
o Levain de MP pra sub-receita "Levain (pé)" nas fichas dos sourdoughs (pro
cronograma agendar o levain na vespera — protecao pos-falta de levain) e a
quantidade SUMIU da massa base da TV do padeiro: a cascata exclui
sub-receitas de proposito (regra dos Danish, onde a sub e montagem). Flag
`Receita.sub_na_amassadeira` (checkbox na ficha da PROPRIA sub; backfill
unico marcou o Levain (pé)) diferencia: com a flag, a sub entra na massa
branca em GRAMAS (qtd × peso_unitario) via
`massa_base.ingredientes_por_porcao`, aparece na cascata/mise en place e o
`rendimento_massa_crua` volta a ser massa/peso (sem a flag o sourdough caia
no rendimento cadastrado 3 em vez de ~4 e o MRP inflava ~25%). Sub SEM a
flag (Massa para folhar) segue fora da massa e com rendimento cadastrado —
NAO mudar. Testes: `tests/test_levain_amassadeira.py`.

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
  retiradas do dia no Painel de Entregas.
- **Destrava de baixas presas (19/07/2026, caso retirada #16 Nebraska presa
  em_transporte 12h+ — "o pessoal esta se perdendo")**: o unico caminho
  em_transporte→recebida era o QR de recebimento (PIN de motorista) e o
  alerta so repetia "escaneie o QR". Agora, na lista `/pedidos/retiradas`
  (admin): **"Confirmar recebimento (manual)"** — mercadoria CHEGOU e
  ninguem escaneou; `devolucao.receber_retirada_manual` credita a industria
  com conferencia por item (PRIMEIRO caminho que escreve
  `quantidade_recebida`; antes o form do QR so pedia PIN e o campo era
  morto), mata QRs pendentes (scan atrasado leva 410, nunca credita 2x) —
  e **"Cancelar (estorna coleta)"** — mercadoria NUNCA chegou/voltou pra
  loja; `cancelar_retirada` agora aceita em_transporte estornando EXATO os
  movimentos da coleta (mov `devolucao_industria_estorno`; divergencia
  devolve o COLETADO; `*_sem_estoque` nao credita). Recebida segue sem
  volta. Auditoria em HandshakeAudit (etapas `manual`/`cancel_estorno`).
  O alerta de WhatsApp (`alertas_operacionais`, cron 30min, janela
  `RETIRADA_PRESA_HORAS`=12) agora traz o LINK e o gesto de destrava
  (APP_BASE_URL); "Precisa de voce hoje" ganhou as pendencias
  `retiradas_presas`/`separados_presos` (mesma verificacao). Manual de
  operacao documenta as duas pontas e a destrava. POS-REVISAO (fixados):
  TODA transicao de status da retirada (coleta QR, recebimento QR,
  recebimento manual, cancelar) agora e CLAIM atomico por UPDATE
  condicional — acao concorrente perde o claim e nao movimenta estoque 2x
  (padrao do Confirmar do Slack); `_coleta_ja_estornada` (LIKE por SUFIXO
  do token) impede o par "estorno generico ret-<id>" + cancelar/receber de
  duplicar credito (receber recusa; cancelar so fecha com aviso); o check
  de idempotencia do `estornar_devolucao` virou sufixo tambem (contains
  fazia ret-1 casar ret-16 e recusar estorno legitimo); audit web em
  sessao isolada. A regra antiga "cancelar so antes da coleta"
  (test_retirada_lista_web) foi SUBSTITUIDA por decisao do dono — o teste
  antigo foi atualizado. Testes: `tests/test_retirada_receber_manual.py`
  (18 casos).
- **Recebimento pela TELA do padeiro, SEM QR (dono 20/07/2026)**: "o
  padeiro deve concluir, porem ele so tem a tela do /padeiro — nao tem
  como escanear". O card da retirada em transporte na TV do padeiro agora
  tem conferencia por item + botao "✅ RECEBI — DAR ENTRADA"
  (`padeiro.retirada_receber` → `receber_retirada_manual(origem=
  'padeiro')` — mesmo motor da destrava admin: claim atomico, grava
  `quantidade_recebida`, guarda contra coleta estornada). O QR pro
  motorista virou ALTERNATIVA ("QR pro motorista" no card); a decisao de
  02/07 "PIN de driver no recebimento" foi SUBSTITUIDA por esta. Audit
  compartilhado `devolucao.auditar_gesto_retirada` (sessao isolada). O
  teste antigo do botao de QR foi atualizado pro contrato novo. Testes:
  secao "Tela do padeiro" em `tests/test_retirada_receber_manual.py`.

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
loja→industria — o original), **vendas** (venda real das lojas +
merma estrutural — MESMA demanda unificada da Fase 0.1, so receita_id) ou
**maior** (max dos dois POR DIA). O firme conta SEMPRE, em qualquer motor.
**DEFAULT = 'vendas' desde 17/08/2026** (dono: "producao da semana
programada baseado no historico de vendas e estoque" + "mesma regua em
tudo" via AskUserQuestion): a tela SEM ?motor=, os fallbacks dos POSTs
JSON (celula/ia) e a sonda `/api/claude/cronograma` abrem em 'vendas' —
mesma regua da automacao (AUTO_ENVIO_MOTOR abaixo). CONSEQUENCIA no
front: o motor viaja SEMPRE explicito (hidden dos forms `campos_dia`/
limpar-edicoes, links da /previsao e `_params_visao` dos redirects) — a
otimizacao antiga "omite quando e o default" viraria bug na troca de
default (escolher 'pedidos' no select voltaria pra 'vendas' no POST).
EXCECAO documentada: o painel legado `/producao/painel` (e o "criar plano
do deficit" dele) segue no default do SERVICE (`balanco_industria(motor=
'pedidos')` — assinaturas de service NAO mudaram; so os defaults de
REQUEST/env mudaram); alinhar o painel e decisao separada.
Param `motor=` atravessa `balanco_industria`/`cronograma_producao`/
`editar_celula`/`aprovar_plano_do_dia`/`enviar_plano_do_dia`/
`decompor_previsao` e a API do assistente (`/api/claude/cronograma?motor=`).
Aprovar/enviar usa o motor DA TELA (mesma regra do equilibrar — senao a
ordem nao bate com o grid visto). Cache do balanco tem motor na chave.
Constante: `previsao_producao.MOTORES_PREVISAO_PRODUCAO`. Testes: secao
"motor de previsao" em `tests/test_cronograma.py` (incl. default da tela
e preservacao da escolha 'pedidos').

## Automacao de pedidos + envio + corte 19h + fornada sab/dom (10/08/2026)

Pedido do dono ("hoje eu tenho que lancar o pedido manualmente... quero que
o sistema faca os pedidos automaticamente de 3 dias na frente"), decisoes
dele via AskUserQuestion: **automatizar TUDO (pedido + envio)** — REVOGA a
regra de 04/07/2026 "enviar ao padeiro e gesto humano" —, motor
**venda+estoque**, corte com **admin passando com aviso**. **CORTE = 19:00
desde 13/08/2026** (nasceu 18:00; o dono mudou no 1o dia real — caso Joao/
Nebraska, as lojas precisavam da hora extra pra revisar o rascunho).
`pedido_corte.HORA_CORTE` e a fonte; os crons acompanham (refresh 18:30 =
30min antes do corte; envio 19:00) — mudar a hora exige mudar os DOIS jobs
do seru_cron junto.

- **Auto-pedidos** (`app/services/auto_pedidos.py::gerar_pedidos_
  automaticos`, cron 06:30 e 18:30 BRT, lock 7758, kill-switch
  `AUTO_PEDIDOS=0`): roda `sugerir_pedidos_por_venda` (o motor da tela
  /pedidos-semana/estoque; `AUTO_PEDIDOS_SEGURANCA_PCT` opcional, valor
  ilegivel vira 0 com WARNING) com offset 1 e materializa D+1..D+3 via
  `pedidos_semana.aplicar_grade` com `user_id=None` — rascunho 'pendente'
  com o marcador padrao. **RE-SINCRONIZACAO REAL (fix da revisao
  13/08/2026)**: o motor recebe `ressincronizar_datas` e trata o rascunho
  do PROPRIO cron como substituivel (fora do `ja_tem` e das entregas
  simuladas) — sem isso, dia ja pedido devolvia sugestao 0 e a quantidade
  congelava na 1a criacao (o refresh pre-corte era um no-op perpetuo; os
  testes so passavam porque mockavam o motor). A "venda do dia" entra no
  refresh via ESTOQUE atual (o sync do Seru drena a cada 15min), nao via
  media (hist fecha em ontem).
  REGRAS DE RESPEITO: (loja, dia) com pedido CRIADO ou MODIFICADO por
  humano (criado_por/modificado_por_id nao-nulos) NUNCA e sobrescrito
  (`_dias_protegidos`); **confirmar (web e copilot) e voltar-status agora
  CARIMBAM modificado_por_id** — o clique "Confirmar" do gerente protege
  o rascunho do re-sync; D+1 sob o corte nunca e tocado; sugestao zerada
  nao cria pedido vazio; pedido ENTREGUE antes da data (entrega
  antecipada) nao protege o dia (mesmo carve-out do motor/aplicar_grade).
  O rascunho pendente JA conta como comprometido no cronograma
  (comportamento existente — e o que puxa a producao 3 dias a frente).
  **COLISAO humano×cron fechada (critico 2 da revisao 13/08)**: os 3
  caminhos humanos de criacao (web /novo, web /sugerir-pedido, copilot
  criar_pedido) agora ADOTAM o rascunho automatico do dia
  (`pedido_merge.rascunho_automatico_aberto`/`adotar_rascunho_automatico`)
  em vez de criar um 2o pedido — item citado SUBSTITUI a quantidade do
  motor (somar seria demanda em DOBRO na ordem enviada no corte; o match cai pra
  FK-sem-estado quando ha UMA linha do item — "45 assado" substitui a
  linha sem-estado do cron, nunca duplica), item do motor nao citado FICA
  (com aviso "MANTIDOS" visivel), status vira 'confirmado' e o carimbo
  protege. Se ja ha pedido humano 'confirmado' E sobrou rascunho do cron
  no mesmo dia, o proximo criar humano CANCELA o rascunho
  (`absorver_rascunho_automatico`); EDITAR a data de um pedido pra cima
  de um dia com rascunho tambem absorve (web e copilot, `excluir_id`
  protege o proprio pedido); e o PROPRIO CRON absorve dias mistos no
  inicio de cada rodada (`_absorver_rascunhos_orfaos` — a dobra nao
  espera gesto humano). A tela pedidos-semana ja sincronizava o rascunho
  com carimbo (aplicar_grade com user_id).
  **Regras da rodada 2 da revisao (13/08, todas com teste)**:
  - **Sugestao que CAI a 0 sincroniza tambem**: item que sumiu da
    sugestao vai na grade com qtd 0 explicita (o `_sincronizar_itens`
    remove) e dia cuja sugestao zerou POR INTEIRO tem o rascunho
    CANCELADO (`rascunhos_cancelados_zero`) — sem isso os 50 velhos
    congelavam no corte e viravam entrega desnecessaria.
  - **Dia MISTO nao e substituivel no motor**: `ressincronizar_datas` so
    destrava (loja, dia) ocupado SO por rascunho do cron; dia com pedido
    humano mantem o rascunho no carry (excluir so a linha dele inflava
    D+2 — reproduzido pela revisao).
  - **CANCELAR e palavra da loja**: web cancelar e copilot cancelar
    carimbam modificado_por_id, e `_dias_protegidos` protege dia com
    pedido CANCELADO por humano — o cron nao ressuscita pedido que a
    loja matou. Quem quiser o pedido de volta lanca na mao. Cancelado
    SEM carimbo (historico antigo, absorcao do proprio cron) nao
    protege.
  - **Sync do cron NUNCA apaga carimbo humano** (`_sincronizar_itens`
    so escreve modificado_por_id=None se ja era None) — na corrida
    "humano adota entre o snapshot e o commit do cron", as quantidades
    podem ser sobrescritas UMA vez mas o carimbo sobrevive e a rodada
    seguinte protege.
  - **Marcador 'Gerado do histórico' tem FONTE UNICA**:
    `pedido_merge.MARCADOR_RASCUNHO_AUTO`/`OBSERVACAO_RASCUNHO_AUTO` —
    pedidos_semana (escrita), previsao_producao (media + ressinc) e
    previsao_acuracia (circularidade) importam de la. NUNCA re-literalar.
  - PENDENCIA ACEITA (design consistente com o fluxo do cronograma, que
    so enxerga origem='cronograma'): plano AVULSO/manual de amanha ja
    enviado ao padeiro NAO suprime o auto-envio do corte — se um dia o
    dono usar plano avulso pra cobrir amanha, viram duas ordens;
    decisao separada.
  **RETROALIMENTACAO (decisao documentada 13/08)**: pedido-maquina que
  SAI de 'pendente' (separado/entregue) ENTRA na media de pedidos —
  exclui-lo pra sempre faria a media (denominador com zeros por data)
  definhar em ~janela_semanas e o motor 'pedidos' subestimar alem de D+3.
  O eco e limitado pela `quantidade_recebida` (conferencia na entrega) e
  medido em `previsao_acuracia.circularidade_pct`. Rascunho ainda
  'pendente' segue fora (exclusao de sempre).
- **Ordem da SEMANA** (`enviar_ordens_da_semana`, dono 17/08/2026:
  "a ordem de producao da semana soltando ela no domingo, meio-dia, ate o
  proximo domingo" + "quanto menos e mais" — SUBSTITUI o envio diario das
  19:00 de 10/08/2026; lock 7759, kill-switch `AUTO_ENVIO_PLANO=0`):
  job DIARIO as 12:00 BRT (`ordens-semana`) que envia ao padeiro a ordem
  de cada dia de AMANHA ate o PROXIMO DOMINGO que ainda nao tem ordem
  enviada. No domingo 12:00 isso abre a semana inteira (seg..dom); nos
  outros dias e REDE — re-preenche dia excluido/disparo engolido por
  deploy (APScheduler nao persiste misfire e o auto-deploy reinicia o
  processo a qualquer hora) e e no-op com a semana de pe. Motor env
  `AUTO_ENVIO_MOTOR`; default **'vendas'** (dono 17/08/2026; env setada
  no Railway MANDA sobre o codigo — dono confirmou que NAO ha env la). O
  🔄 automatico (abaixo) usa o MESMO fallback — mudar um exige mudar o
  outro. Dia JA ENVIADO (humano ou cron) e PULADO — "ordem enviada nunca
  muda por caminho implicito"; `PlanoJaEnviadoError` no aprovar (humano
  enviou na corrida) tambem pula so aquele dia. Dia sem nada no grid =
  `vazias` (nada criado). CONSEQUENCIAS deliberadas: (1) pra TIRAR um
  dia da producao, zera-se o grid (envio de dia vazio limpa a ordem) —
  EXCLUIR a ordem faz o meio-dia seguinte reenvia-la do grid; (2) a
  pre-baixa de MP reserva a SEMANA inteira no envio (mesma semantica de
  sempre do enviar — o 🔄 diario reconcilia o delta); (3) ordem de dia
  futuro fica com numero do domingo ate o 🔄 do proprio dia — o padeiro
  executa so a ordem DE HOJE, que o 🔄 mantem em dia (06:45/19:05), e a
  tela mostra "difere do enviado" nos demais. RETRO one-shot no deploy
  de 17/08/2026: job 'date' no boot (+2min) roda a 1a semana com marker
  AppConfig `ordens_semana_retro_2026_08_17` (falhou = proximo boot
  retenta; rodou = deploys futuros pulam).
- **Corte do fim do dia** (`app/services/pedido_corte.py`, HORA_CORTE=19
  desde 13/08/2026): pedido com
  `data_entrega == amanha` trava as 19:00 BRT — e o horario do
  PRE-PREPARO do padeiro (preparar.json calcula a vespera). Gerente/
  funcionario/producao/padeiro BARRADOS; admin/owner passa com AVISO.
  Defesa em profundidade (padrao da trava de MP): web novo (ANTES do
  merge — criar podia virar mesclar num pedido de amanha), editar (data
  ATUAL e NOVA — mover pra/tirar de amanha fura igual), cancelar, e
  executores do copilot (preview re-enviado nao fura; admin ganha
  `aviso` no resultado). LIMITACAO ACEITA: apos a meia-noite o pedido
  (agora "de hoje") volta ao regime normal — o corte protege a janela
  HORA_CORTE-00:00; loja nao opera de madrugada.
- **Fornada especial vende SO sab/dom** (dono 10/08/2026; SUBSTITUI
  qui/sex/sab->sex/sab/dom de 06/07/2026): `_DIAS_FORNADA_ESPECIAL =
  {5,6}` e `_DIAS_PRODUCAO_FORNADA = {4,5}` em previsao_producao.py.
  Textos das telas (teste.html, ficha.html, msg do cronograma_edit)
  acompanharam. Pedido FIRME lancado pra outro dia segue contando (firme
  nao passa pelo gate de venda). Testes da secao reescritos.
- **🔄 AUTOMATICO da ordem do dia** (`atualizar_plano_automatico`, cron
  06:45 e 19:05 BRT, lock 7761, mesmo kill-switch `AUTO_ENVIO_PLANO=0` —
  criado 17/08/2026, caso real do 1o fim de semana: "as ordens nao estao
  sendo enviadas" = a ordem de segunda saiu domingo 19:00 com 3 itens/
  3.274 un e o grid do proprio dia amanhecia pedindo 8 itens/6.577; os
  itens de VESPERA da ordem — levain, lead-1, pre-preparo — sao dirigidos
  pela demanda de AMANHA, que o cron de pedidos re-sincroniza 06:30/18:30
  DEPOIS de a ordem congelar; antes da automacao o dono dava o 🔄 na mao).
  Re-envia a ordem DE HOJE pelo `enviar_plano_do_dia` (o mesmo gesto do
  botao 🔄) SO quando ela foi criada pelo PROPRIO CRON (`criado_por`
  None) — ordem enviada por humano segue intocavel. 06:45 = pos-refresh
  da manha; 19:05 = pos-corte (demanda de amanha congelada, numero final
  pra madrugada). Diagnostico foi pela sonda NOVA
  `/api/claude/ordens-producao?de=&ate=` (criada no caso: lista
  PlanejamentoProducao com criado_em/criado_por/enviado — "o cron enviou?"
  responde-se de fora; o envio das 19:00 SEMPRE disparou, o problema era
  conteudo estagnado). O motor dos 3 (`pedidos`/`vendas`/`maior`) foi
  comparado no caso e muda pouco o resultado — o gargalo era o relogio,
  nao o motor.
- **Corte tambem no copilot cancelar** (fix achado 4 da revisao 13/08):
  `executar_mudar_status_pedido('cancelar')` agora passa pelo
  `bloqueio_do_corte` (a rota web ja passava); admin ganha `aviso`. Toda
  transicao de status via copilot carimba modificado_por_id. A tela
  pedidos-semana (admin) ganhou o aviso do corte no gerar (flash + msg do
  ajax).
- Testes: `tests/test_auto_pedidos.py` (18 — inclui 2 com o MOTOR REAL
  travando a re-sincronizacao e os 3 de colisao/adocao),
  `tests/test_pedido_corte.py` (12), secao fornada de
  `tests/test_cronograma.py` reescrita. Manual de operacao atualizado
  (RODA SOZINHO + DIARIO).

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

## Falta encerrada pelo padeiro (17/07/2026)

Decisao do dono: o padeiro que produz MENOS que o alvo pode dar o item por
FEITO — a tela dele para de cobrar; a diferenca vive SO na auditoria.

- `PlanejamentoItem.falta_encerrada_em` (ALTER em migrations_legacy,
  procedimento 2 commits). Diferente de `dispensada_em`: NAO bloqueia
  produzir, NAO sai da auditoria, NAO libera pre-baixa de MP.
- Fluxo: lancamento PARCIAL no /padeiro → 2º confirm ("encerrar? OK/
  Cancelar continua em levas") → `produzir_item_plano(..., encerrar=True)`
  marca se restar falta. Telas do padeiro filtram o marcador igual ao
  dispensado (`_plano_do_dia`); estoque credita so o produzido.
- Auditoria: selo "encerrado pelo padeiro" (vencidas+agendadas); admin
  decide — ✓ OK = `dispensar_item` (existente) ou `reagendar_para_hoje` =
  devolve a falta pra tela do padeiro. O reagendar LIMPA o marcador no
  merge (mesma armadilha do dispensado reaberto — item oculto engoliria a
  falta devolvida) e no item de origem.
- Testes: `tests/test_padeiro_encerrar_falta.py` (8 casos).

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

**Checkout CEP-first + retentativa com logradouro OFICIAL (17/07/2026, caso
Mirelle)**: venda barrada 2x — cliente digitou "Rua Cândido de Azevedo
Marques" (o oficial tem "JOAQUIM" na frente; nenhum geocoder achava) e depois
o CEP com os digitos INVERTIDOS (88650-020 em vez de 05688-020, CEP de SC).
O autofill por CEP ja existia mas so disparava no blur, falhava em SILENCIO
(404/erro = return sem aviso) e os campos ficavam livres. Duas camadas,
decisao do dono ("bloquear o campo endereco"):
- **Checkout CEP-first** (`checkout.js`): logradouro/bairro/cidade/UF ficam
  READONLY (nunca disabled — disabled nao submete no POST) ate o CEP
  resolver; lookup dispara no `input` ao completar 8 digitos (autofill de
  navegador nem sempre da blur) + blur como rede; `#cep-status` acima do
  grid mostra buscando/preenchido/erro. FAIL-OPEN obrigatorio: API de CEP
  fora (502) ou CEP sem rua na base (CEP geral) DESTRAVA os campos — venda
  NUNCA fica presa por infra. 404 (CEP nao existe) mantem travado com aviso
  "confira o numero" + botao `#cep-corrigir` (saida de emergencia, tambem
  apos preencher). GRANDFATHER: endereco ja preenchido no load (conta com
  endereco salvo / re-render pos-erro do POST) NAO trava. Corrida com o
  "Calcular frete": clique durante lookup em voo vira `freteAposCep=true` e
  re-dispara sozinho ao terminar.
- **`api_cep` com fallback ViaCEP** (`loja/routes.py`): BrasilAPI primeiro;
  falhou por INFRA → ViaCEP. Contrato com o front: 404 = CEP NAO EXISTE
  (mantem travado) ≠ 502 = infra fora (fail-open). Com CEP-first, a rota
  fora do ar sem fallback viraria venda travada no site inteiro.
- **Retentativa Google com o logradouro OFICIAL** (`frete.py::
  _geocodificar_impl`): quando a BrasilAPI conhece o CEP mas sem coordenada,
  re-tenta `_google_geocode` com "rua oficial + numero + bairro + cidade +
  CEP" ANTES da cadeia Nominatim (`ref['rua']` novo em `_geocodificar_cep`).
  Mesmo teto/cache/kill-switch do Google. Provado na sonda: o texto da
  Mirelle falhava; com "Joaquim" resolvia (1,9km, R$5). Guard: CEP sem rua
  nao re-tenta (bairro/cidade viram centroide que o geocode_preciso rejeita
  — chamada paga inutil).
- **Bug pre-existente achado na validacao**: o reset mobile do
  `.endereco-grid` (@media 560px em `loja.css`) PERDIA por especificidade
  pros `label:nth-child(...)` desktop (0-1-1 vs 0-2-1) — NUNCA aplicou
  desde que foi escrito; o span desktop vazava, criava 3ª coluna implicita
  e o campo CEP rendia com ~51px no celular. Fix: reset com
  `label:nth-child(n)` (empata 0-2-1, vence por ordem). Ao criar override
  mobile de regra que usa nth-child, conferir especificidade.
Pos-revisao (fixados): so `status_code == 404` da BrasilAPI marca "CEP nao
existe" — 429/5xx e INFRA e vira 502/fail-open se o ViaCEP tambem cair
(teste `test_api_cep_5xx_da_brasilapi_nao_vira_404`); `reconferirCep` fecha
a corrida "CEP corrigido durante lookup em voo" (a resposta velha nao pode
deixar endereco de A com CEP B no campo); mascara so reatribui se mudou
(cursor) e o foco so pula pro numero se ainda estiver no CEP. Trade-offs
ACEITOS: trocar o CEP depois do "corrigir manualmente" re-busca e RE-TRAVA
por cima do que foi digitado (CEP novo = endereco novo; o link reaparece);
retentativa canonica pode gastar 2 slots do teto Google no pior caso.
Testes: `test_google_retenta_com_logradouro_oficial_do_cep` +
`test_cep_sem_logradouro_nao_retenta_google` em `tests/test_frete.py`;
5 casos de fallback/404/5xx/502 em `tests/test_loja_checkout_v2.py`.
Validacao visual/funcional Playwright 390px (14 checks, incl. fail-open).

**RETIRADA tambem coleta endereco pra NF-e (20/07/2026, dono)**: pedido de
retirada nascia SEM endereco estruturado (`loja_checkout` so montava a linha
"Retirada: loja"; os `endereco_logradouro/numero/bairro/cidade/uf` ficavam
NULL) — a NF-e do Tiny saia com o destinatario em branco e a SEFAZ rejeitava
("endereco/bairro/UF em branco", caso pedido 5d51be2f). Agora o ramo
`modo=='retirada'` do `criar_pedido` VALIDA e grava o endereco (exige o
CONJUNTO que a SEFAZ pede — cep/logradouro/numero/**bairro**/cidade/**uf**,
NUMERO obrigatorio; MAIS que a entrega, que nao exige bairro/uf — no caminho
feliz vem READONLY do CEP, e exigir fecha a armadilha do fail-open pq a
retirada nao tem editor de endereco no admin e a NF travaria pra sempre no
guard — achado de revisao), mas SEM
recalcular frete nem geocodificar: o endereco serve SO pra nota, a retirada
continua na loja e o frete fica R$0; a linha legivel `endereco_entrega`
segue mostrando a loja pra operacao. No checkout, o bloco de endereco
(compartilhado) passa a aparecer na retirada com titulo "Seu endereco (para
a nota fiscal)" — o JS (`aplicarModo`) esconde so as partes de entrega
(quem recebe / calcular frete) e mostra o aviso da NF. Defesa fiscal:
`tiny_nf._endereco_destinatario_incompleto` fail-close a emissao do SITE
(nao manda destinatario em branco pra SEFAZ — recusa com mensagem clara em
vez de rejeicao criptica; guard so no caminho do site, B2B/transf tem
fonte de endereco propria) e `numero` vazio vira 'SN'. Decisao do dono:
pedidos JA PAGOS sem endereco (o 5d51be2f) NAO ganharam editor no admin —
resolver no painel do Tiny. Fix cosmetico junto: `loja_online_pedido_
detalhe.html` renderizava o literal "None" quando `Loja.endereco` era NULL
(guard adicionado) + mostra o endereco da NF do cliente. Testes:
`tests/test_loja_checkout*.py` (forms de retirada ganharam `_END_NF`;
`test_criar_pedido_retirada_coleta_endereco_pra_nf` +
`_sem_endereco_falha`), `tests/test_loja_emitir_nf.py`
(`_bloqueia_sem_endereco`, `_numero_vazio_vira_SN`). Validacao Playwright
390px (retirada mostra endereco+loja+aviso, entrega reverte, sem estouro).

## Divulgação — pedido "como do site" SEM pagamento (21/07/2026)

Pedido do dono: lançar um pedido igual ao do site (destinatário, entrega ou
retirada, data, itens) mas SEM etapa de pagamento (brinde/PR), que aparece no
`/entregas/painel` como um pedido normal marcado com ESTRELA ⭐. Decisões do
dono (AskUserQuestion): **baixa o estoque físico** (o pão sai pela porta) mas
MARCADO como divulgação (fora de faturamento e da previsão de venda) +
**tela admin nova**.

- **Schema (2 commits)**: `PedidoOnline.divulgacao BOOLEAN NOT NULL DEFAULT
  FALSE` (ALTER em `migrations_legacy` deployado ANTES do modelo, sonda
  `/api/claude/deploy`).
- **Motor**: canal `'divulgacao'` novo em `baixa_venda._MOVS`/`_SINAL_ESTORNO`
  com tipos PRÓPRIOS `venda_site_divulgacao*`. Como a previsão só soma
  `tipo IN VENDA_TIPOS_DEMANDA_COM_ESTORNO` (`previsao_demanda.py`) e esses
  tipos NÃO entram na whitelist, a divulgação sai da previsão de venda
  automaticamente — e fica rastreável no ledger.
- **Serviço** `app/services/divulgacao.py`: `criar_divulgacao(...)` cria o
  `PedidoOnline` (status `'divulgacao'`, `pago_em=NULL`, `divulgacao=True`,
  sem NF/cobrança/e-mail) e baixa o estoque pelo motor único (explode cesta/
  fração igual à venda do site, tolera shortfall). Loja de baixa = MESMA regra
  do site (`retirada` baixa da escolhida; entrega/express de
  `loja_origem_site()`). `cancelar_divulgacao(pedido)` estorna o estoque e
  marca `cancelado`.
- **Faturamento**: o site conta por `pago_em` e a divulgação nasce com
  `pago_em=NULL` → já sai naturalmente; guard explícito da flag
  (`PedidoOnline.divulgacao.is_(False)`) em `briefing_dono.vendas_hoje/ontem`
  e `chatbot_auditor._funil_site` (documenta/blinda; o funil também exclui do
  `pedidos_criados`).
- **Painel/PDF**: `_serializar_pedido_online` expõe `divulgacao`; status
  `'divulgacao'` entra em `_STATUS_ONLINE_NO_PAINEL`/`_STATUS_ONLINE_PARA_PAINEL`;
  selo dourado ⭐ DIVULGAÇÃO no card (`painel_pedidos.html`) e "DIVULGAÇÃO
  (CORTESIA)" no PDF do motorista (`pdf.py`).
- **Tela** `/admin/loja-online/divulgacao`: destinatário, entrega OU retirada,
  data+janela, itens. Detalhe do pedido mostra faixa ⭐ + botão "Cancelar
  (devolve estoque)". Registrado no manual (QUANDO PRECISAR).
- **GATE = SÓ owner + papel `marketing`** (dono 21/07/2026: "só o owner e
  marketing"). Papel novo `marketing` (`constants.PAPEIS_VALIDOS`; `Usuario.
  is_marketing()`/`pode_divulgacao()` = dono OU marketing; decorator
  `divulgacao_required`). Admin comum NÃO entra. `main.index` redireciona o
  marketing direto pra tela de divulgação (papel enxuto, sem outras áreas); a
  sidebar mostra só o link de divulgação pra ele (esconde catálogo/pedidos que
  dariam 403). Marketing não acessa o detalhe do pedido (gerente_required) —
  ao criar, volta pro form com flash de sucesso; admin/gerente vão pro detalhe.
  Opção "Marketing" no `/auth/usuarios`.
- **UX do form (dono 21/07)**: TODOS os campos obrigatórios (validação client
  togglada por modo + server no `criar_divulgacao` — ValueError claro, nada
  criado sem tudo). Itens por **typeahead** client-side (combobox filtra o
  catálogo embutido por texto; hidden `item_alvo[]` só com item escolhido —
  digitar limpa a seleção pra evitar lixo). **CEP autofill** reusa
  `/loja/api/cep/<cep>` (alcançável do host gestão) preenchendo logradouro/
  bairro/cidade/uf; a `endereco_entrega` (snapshot do painel/motorista) é
  montada dos campos estruturados no route.
- **Data mínima POR PAPEL (dono 08/08/2026: "eu como owner devo conseguir
  lançar para quando quiser")**: `criar_divulgacao(permitir_hoje=...)` — o
  DONO (`is_dono()`, ligado na rota) lança pra **HOJE** em diante (passado
  recusado pra todos: não há o que entregar ontem); o papel `marketing`
  segue a regra original de 21/07 (**≥ amanhã**). O `min` do calendário
  acompanha o papel (`data_min`); o `value` default continua amanhã. As
  janelas de HOJE vêm cortadas pelo horário (mesma
  `janelas_disponiveis` do site) — dono lançando à noite pode não ter
  janela restante pra hoje, e aí só amanhã mesmo. Testes:
  `test_dono_pode_lancar_pra_hoje`, `test_nem_o_dono_lanca_pro_passado`,
  `test_rota_post_owner_pra_hoje_cria`,
  `test_rota_post_marketing_pra_hoje_nao_cria`. A janela virou **select dinâmico** preenchido pelo
  endpoint `/admin/loja-online/divulgacao/janelas`, que replica a MESMA regra
  do site (`loja_checkout.janelas_disponiveis`): agendada corta a 1ª janela da
  manhã (08:00–09:00) quando o endereço está longe (distância do
  `frete.consultar_frete`; fail-open se o geocode falhar → todas as janelas);
  retirada sem distância. O modo **express foi removido** do form (é same-day,
  conflita com "nunca no mesmo dia").
- Testes: `tests/test_divulgacao.py` (18 casos, inclui gate por papel). NUNCA
  fazer a divulgação contar como venda nem usar o tipo `venda_site` (mataria a
  distinção), e NUNCA abrir o gate pra admin comum sem ordem do dono.

## Produto SOB ENCOMENDA D+2 no site (21/07/2026)

Pedido do dono: alguns produtos do site são **sob encomenda** — só podem ser
pedidos com **2 dias de antecedência** (ex.: mini pain au chocolat; comprou na
segunda → entrega/retirada válida só a partir de quarta, desde a janela das
08:00). Decisões do dono (AskUserQuestion): **produzido pro pedido** (NÃO abate
a prateleira, sempre disponível na vitrine) + **igual a B2B** (entra na produção
do padeiro: separação + cronograma + pré-preparo) + **lead FIXO D+2**.

- **Schema (2 commits, sonda /api/claude/deploy)**: `Receita.sob_encomenda` +
  `Produto.sob_encomenda` BOOLEAN NOT NULL DEFAULT FALSE (ALTER em
  `migrations_legacy` PG+SQLite deployado ANTES do modelo). Checkbox na ficha
  da receita e no cadastro do produto; `duplicar` copia a flag.
- **Constante** `loja_checkout.ENCOMENDA_LEAD_DIAS = 2` (FIXO, não varia por
  receita). Helper `loja_checkout.lead_do_carrinho(itens)` = 2 se QUALQUER item
  é sob encomenda, senão 0 (o carrinho todo herda o MAIOR lead — uma data de
  entrega por pedido; misturar item normal + encomenda empurra tudo pra D+2).
- **Data D+2 no checkout**: `datas_disponiveis(modo, ..., lead_dias=N)` — com
  N>0 a 1ª data válida vira `hoje + N` (sem hoje/amanhã; dia futuro → todas as
  janelas a partir das 08:00). `criar_pedido` computa o lead do carrinho, valida
  a data contra ele e **bloqueia express** (same-day conflita com D+2). O
  `_ctx_checkout` (loja/routes) seta o `min` do calendário e esconde o express
  quando o carrinho tem encomenda (`encomenda_no_carrinho`); checkout.html mostra
  aviso e produto.html um selo "📅 sob encomenda".
- **Vitrine: era "SEMPRE disponível" — SUBSTITUÍDO em 07/08/2026 (dono:
  "Quero controlar tudo numa só tela", caso Caixa de Mini no Dia dos
  Pais)**: sob encomenda agora RESPEITA o plano-do-dia como qualquer item —
  `tem_estoque_para_dia` consulta o plano (sem bypass), `anotar_esgotado`
  calcula esgotado pela janela >= D+2, o loop de esgotados do
  `criar_pedido` não pula mais a flag, `loja_pagamento._reservar/_devolver_
  ao_plano_do_dia` reservam/devolvem quantidade (sem isso o cap do plano
  não seguraria nada) e `bot_tools._datas_indisponiveis` expõe dia curado
  de encomenda pro bot/vigia. Sem plano cadastrado segue fail-open
  (disponível). O que NÃO mudou: continua produzido pro pedido (nunca
  abate/reserva EstoqueLoja físico — `_rebaixar_pedido` e
  `loja_estoque_reserva` seguem pulando), D+2, produção firme (2c) e card
  do padeiro. Pedido antigo (nunca reservou plano) cancelado após o
  deploy: `devolver` trunca em 0/no-op — não cria saldo fantasma (caso
  raro de linha com reservas novas: aceito). Testes: seções novas em
  `test_sob_encomenda.py` + contrato novo em
  `test_vigia_disponibilidade_por_data.py`. PENDÊNCIA (achado de revisão,
  decisão separada): o botão "Reparar órfãs" do plano-do-dia
  (`loja_plano_dia.reparar_linhas_orfas`) trata TODA linha `(planejada=0,
  reservada>0)` como bug pré-24/06 e sobe pra 99999 — com encomendas
  reservando plano, esse estado virou LEGÍTIMO (venda antes da curadoria +
  dono zera o dia) e o clique REABRIRIA o dia curado. Não clicar após
  curar um dia; distinguir órfã real exigiria marcador novo. Também
  pré-existente: divulgação não consome plano
  (`divulgacao.criar_divulgacao` — by design, gesto de owner).
- **NÃO abate EstoqueLoja (produzido pro pedido)**: `loja_estoque_reserva.
  item_sob_encomenda(item)` faz `_expandir_estoque` retornar `[]` (fora de
  reserva/liberação) E o loop de baixa real do `consumir` PULA o item (as DUAS
  pontas — a baixa real NÃO passa pelo `_expandir_estoque`, achado do 1º teste).
  `loja_pagamento._reservar/_devolver_ao_plano_do_dia` também pulam (fora do
  plano-do-dia). Item do site NÃO sob encomenda continua abatendo normalmente
  (teste-guarda). ATENÇÃO: a DIVULGAÇÃO tem baixa própria (canal 'divulgacao')
  e NÃO é afetada por isso — de propósito (brinde sai pela porta).
- **Produção (padeiro + cronograma)**: pedido PAGO (status pago/em_preparo/
  a_caminho — nunca aguardando_pagamento/cancelado/entregue; divulgação fora)
  com item sob encomenda:
  - **Balanço firme** (`previsao_producao.balanco_industria`, bloco "2c",
    espelho do B2B `_contrib_b2b`): SÓ os itens sob encomenda viram demanda
    firme por receita (cesta explode em receita). É ADITIVO e sem risco de
    dobra — o site não é lido em nenhum outro ramo do balanço e o item não
    baixa EstoqueLoja. Linha própria "Encomenda site" no
    `breakdown_comprometido`. Alimenta o cronograma → ordem do padeiro.
  - **Separação do padeiro** (`padeiro._dados_listas` + `_card_online`, tipo
    `'online'`): card DOURADO informativo na fila "a separar" (sem botão
    SEPARAR — a entrega do site roda pelo /entregas/painel; aqui é só garantir
    a produção). Mostra SÓ os itens sob encomenda. **Na visão de HOJE entram
    TAMBÉM as encomendas de data FUTURA** (fix 31/07/2026, caso real: menu de
    minis vendido na sexta pra entrega no domingo e a TV ficou muda até
    domingo — o sob encomenda existe JUSTAMENTE pra produzir com
    antecedência, o cronograma agenda fornadas dias antes): o card aparece do
    pagamento até virar entregue/cancelado, com a data de entrega visível.
    **Item de MENU CONFIGURÁVEL explode na COMPOSIÇÃO ESCOLHIDA** (mesmo fix;
    fonte = `composicao_escolhida`, a mesma do bloco 2c) — antes o card e o
    pré-preparo diziam só "1x Menu Degustação dos Minis" e o padeiro não
    tinha como produzir; agora listam "20x Mini Nutella, 10x Mini Danish...",
    e no pré-preparo cada mini sai com o SEU `estado_padrao`. Testes: seção
    "Fixes 31/07/2026" em `tests/test_sob_encomenda.py`.
  - **Pré-preparo** (`padeiro.preparar_json`): itens sob encomenda com
    `data_entrega == alvo` (dia+1) entram no pré-preparo da véspera; estado =
    `Receita.estado_padrao` (assado/backup) ou 'assado' de fallback pra sempre
    aparecer.
- Sonda `/api/claude/receita` expõe `sob_encomenda`. Testes:
  `tests/test_sob_encomenda.py` (19 casos). NUNCA fazer sob encomenda abater
  EstoqueLoja nem contar como venda de prateleira; NUNCA permitir data < D+2 ou
  express; a produção é a única forma de atender.
- **PÓS-REVISÃO (fixado)**: `loja_pagamento._rebaixar_pedido` (correção owner
  "reduzir qtd de pedido pago") re-baixava TODOS os itens, recriando a baixa
  fantasma do item sob encomenda — que aí contaria 2x (`venda_site` no
  motor=vendas + firme no 2c). Agora pula sob encomenda (mesma guarda do
  `consumir`) e o `reduzir_item_pedido_pago` também não devolve plano-do-dia
  pra item sob encomenda (nunca reservou). O rótulo do express no checkout
  explica o bloqueio por encomenda; pré-preparo faz eager-load de `produto`
  (sem N+1). Testes: `test_rebaixar_pedido_pula_sob_encomenda`.
- **LIMITAÇÕES ACEITAS (baixa severidade, achados de revisão — decisão
  separada)**: (1) receita sob encomenda usada como COMPONENTE de uma cesta
  NÃO sob encomenda ainda abate EstoqueLoja (a flag só é checada no item de
  topo do `_expandir_estoque`; itens sob encomenda são vendidos DIRETO, não
  dentro de cesta); (2) receita sob encomenda ARQUIVADA com pedido pago
  pendente some do balanço (`receitas` exclui arquivadas) mas segue no card do
  padeiro — mesma classe do B2B pré-existente; (3) `lead_do_carrinho` (front,
  `item_e_sob_encomenda` sem filtrar arquivada/preço) pode mostrar `data_min`
  D+2 pra um item que `montar_itens` descarta — direção SEGURA (front mais
  restrito que o servidor, que recalcula lead=0 e o item cai fora).

## Menu degustação CONFIGURÁVEL no site (26/07/2026)

Pedido do dono: "menu degustação dos minis, uma **pré-seleção de 5 de cada**.
Porém se o cliente quiser alterar as quantidades não tem problema; porém ele
deve ser obrigado a selecionar **30 unidades** dos minis independente de
quais". Decisões dele (AskUserQuestion): **preço varia conforme a escolha,
cadastrado POR MINI** (o menu custa a soma do escolhido) e regra **só pra
esse menu** (não vira comportamento global de cesta). O "máximo 10 de cada"
foi **REVOGADO no mesmo dia** ("quero tirar a regra de 30/10, quero que
tenha 30 unidades do mini independente de quantos sejam"): `menu_max_por_item`
em branco = **SEM teto** (dá pra fechar as 30 com um mini só). Quem quiser
limitar preenche o campo — NÃO reintroduzir o default 10.

- **Modelagem**: é uma cesta NORMAL (`Produto` + `ProdutoItem`) com 3 colunas
  novas no Produto (`menu_configuravel`, `menu_total_unidades`,
  `menu_max_por_item`) e 1 no ProdutoItem (`preco_menu`) — procedimento de
  2 commits, ALTER confirmado pela sonda `/api/claude/deploy` antes do
  modelo. `ProdutoItem.quantidade` do cadastro = **pré-seleção**.
- **`preco_menu` mora no ProdutoItem, NÃO na Receita**, de propósito: os
  minis não são vendidos avulsos e um `preco_site` neles os PUBLICARIA na
  vitrine (`produtos_publicados` usa `preco_site > 0` como flag). Efeito
  colateral desejado: o preço é por-menu, então o mesmo mini pode valer
  diferente em menus diferentes.
- **`Produto.preco_site` do menu vira só o INTERRUPTOR de publicação** — o
  preço exibido é o **MÍNIMO possível** ("a partir de", decisão do dono
  26/07: "esse valor do cardápio, inclusive no site, deveria ser o valor a
  partir de"). `loja_menu.preco_minimo` enche o total com os mais baratos
  respeitando o teto — é o piso REAL, nenhuma montagem válida sai por menos.
  Vale também no **cardápio** (tela e PDF), onde o `preco_atacado/loja/site`
  do cadastro passa a ser IGNORADO no menu (o `preco_menu` é único).
- **Carrinho e checkout têm que calcular o preço PELA MESMA ROTA**
  (`normalizar` + `preco`, com `comp` do jeito que estiver). Já quebrou:
  linha sem `comp` exibia o mínimo no carrinho e era cobrada pela
  pré-seleção — R$ 300 na tela, R$ 360 na fatura (achado de revisão
  26/07/2026). `preco(produto, {})` devolve **None**, nunca `Decimal('0')`:
  composição vazia é escolha invalidada, não item de graça.
- **Menu NÃO tem quick-add em card nenhum** (vitrine e "Monte sua cesta"):
  o card vira link "Montar o meu →". Um "+ Adicionar" cria linha SEM
  composição — cai no bug de preço acima e duplica a linha de quem montou.
  `itens_para_montar` também exclui menu na origem. **Menu com algum mini sem `preco_menu` SAI DA VITRINE**
  (fail-close com WARNING; `por_id_publicado` devolve None) — não vendemos
  sem saber o preço (dinheiro tem peso especial).
- **Endereçamento pelo `produto_item_id`**: a escolha viaja como
  `{produto_item_id: qtd}`. O cliente NUNCA manda `receita_id` — POST
  forjado não consegue enfiar no menu um item que não é dele. Slot de outro
  menu é descartado em `loja_menu.normalizar`.
- **Servidor é a autoridade** (`loja_menu` + `loja_checkout.montar_itens`):
  re-sanitiza contra o cadastro, clampa no teto, exige o total EXATO e
  RECALCULA o preço. Total errado (aba parada, carrinho velho, POST forjado)
  **sai do pedido com aviso** — nunca "conserta" em silêncio. O JS
  (`MenuMontador` em `carrinho.js`) é só conveniência de tela.
- **ESTOQUE — a regra que não pode regredir**: a composição escolhida é
  PERSISTIDA no pedido (`PedidoOnlineItemComponente`, tabela nova via
  `db.create_all`) e é ELA que a reserva (`loja_estoque_reserva.
  _expandir_estoque` → `composicao_escolhida`) e a baixa real (`consumir` →
  `aplicar_venda(composicao=...)`, param novo no motor único) explodem —
  **NUNCA o cadastro da cesta**, que guarda só a pré-seleção e debitaria a
  composição errada. Reserva e baixa mexem nas MESMAS linhas (senão
  `quantidade_reservada` não zera). `_rebaixar_pedido` e o bloco 2c da
  previsão espelham. Cesta de composição FIXA segue pelo cadastro (há teste
  de regressão travando isso).
- **Chave da linha do carrinho inclui a composição** (`carrinho.js::
  _chaveComp` / `loja_menu.chave` / `routes._set_carrinho_sessao`): dois
  menus montados DIFERENTE são linhas separadas — sem isso somariam
  quantidade numa linha só e o cliente receberia dois iguais. Os 3 lugares
  têm que concordar nas classes de equivalência (a string não precisa ser
  idêntica; a ordenação é numérica no Python e no JS).
- **Cookie de sessão**: a composição viaja nele. Orçamento
  `_CARRINHO_MAX_PARES_COMP = 120` pares somados no carrinho inteiro (~1KB
  dos ~4KB). Linha que estouraria é RECUSADA inteira — nunca entra sem a
  composição (sem ela o servidor cairia na pré-seleção e o cliente receberia
  outra coisa).
- **`PedidoOnlineItemComponente.produto_item_id` é Integer PURO, sem FK**:
  `produtos.salvar_composicao` APAGA e RECRIA todos os `ProdutoItem` a cada
  salvamento — uma FK real bloquearia o admin de editar o menu assim que
  existisse um pedido. Quem manda na baixa são as FKs de alvo (estáveis).
  O `preco_menu` sobrevive ao recriar por grandfather `(tipo, nome)`.
- A composição escolhida aparece no carrinho/drawer/checkout, no e-mail de
  confirmação, no **painel de entregas**, no **PDF do motorista** e no
  detalhe admin do pedido — a cozinha separa pelo que o cliente montou.
- Testes: `tests/test_menu_configuravel.py` (~40 casos). Manual de operação
  registrado (seção QUANDO PRECISAR).

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

### Acerto de DESPACHO DIRETO da industria (08/08/2026, Dia dos Pais)

Aviso do dono na vespera: "no dia 9 os itens dos pedidos sairao diretamente
da industria e isso interfere no estoque". Auditoria (3 agentes, codigo +
sondas) confirmou a DISTORCAO DUPLA:
- A baixa do site e no PAGAMENTO (`loja_pagamento.py:488`), nunca na
  entrega — os ~106 pedidos pagos do dia 9 (104 entrega + 2 retiradas)
  drenaram o EstoqueLoja da ANESIO ao longo da semana (6d: 255 croissants,
  258 pains, 179 cookies, 160 almonds... de saldo REAL; peito de peru/mel/
  bases MDF cairam 100% em `sem_estoque` = so ruido) por mercadoria que
  NUNCA passou na prateleira dela.
- Pedido do site NAO debita a industria em NENHUM caminho (zero ocorrencias
  de EstoqueProducao na cadeia pagamento→baixa) — a producao do evento fica
  creditada (1.161 croissants em estoque na industria) e sai sem debito:
  infla ~1.818 un e o cronograma da semana seguinte SUPRIME producao
  (produzir = alvo − estoque).
- Cestas comuns do site sao INVISIVEIS ao balanco da industria por desenho
  (bloco 2c so cobre sob_encomenda) — data especial repete a cegueira se
  ninguem conferir os pedidos pagos a mao (a aba Produtos/XLSX e o gesto).

**Decisao do dono (AskUserQuestion): ajuste CIRURGICO por pedido** — rejeitado
o PedidoLoja retroativo (poluiria a media da previsao de pedidos) e a
conferencia manual (mistura com sobras reais). Ferramenta:
`app/services/acerto_despacho.py::acertar(data, executar=False)` + rota
owner `GET /admin/acerto-despacho?data=YYYY-MM-DD[&executar=1]`:
- **Credito na loja** POR PEDIDO via `loja_pagamento._estornar_estoque`
  (motor unico; respeita versao da baixa e fracoes; devolve SO o que saiu
  de saldo real — sem_estoque nao credita nada).
- **Debito na industria** pela composicao FISICA despachada (cesta explode;
  menu pela composicao ESCOLHIDA; **sob_encomenda ENTRA no debito** — saiu
  da industria — embora nunca tenha baixado loja): EstoqueProducao via
  `obter_linha_producao` (mov tipo `saida_site_direto`) e MP via
  `MateriaPrima.estoque_atual` (espelho do `baixar_industria_pedido`);
  falta NUNCA vira saldo negativo, fica anotada nos avisos.
- **Idempotente POR PEDIDO** (AppConfig `acerto_despacho_<data>` = JSON de
  codigos): re-rodar so pega pedidos novos; a fase 1 do `estornar_venda`
  (inteiros) exige chamada unica e e o marcador que garante. Transacao
  unica com rollback. Marker ILEGIVEL = ValueError alto (degradar pra
  vazio re-creditaria tudo em silencio).
- A rota RECUSA `executar=1` com data >= hoje (mercadoria precisa ter
  saido — executa a partir do dia SEGUINTE, de proposito: cancelamentos
  tardios saem sozinhos); dry-run e livre.
- **POS-REVISAO (fixados, 08/08/2026)**: (1) claim de execucao — advisory
  lock GLOBAL 7757 + `serializar_lojas` ascendente ANTES de ler o marcador
  (rodada concorrente espera, rele e vira no-op; ordem canonica nao
  deadlocka com o sync do Seru); (2) estornos RE-DATADOS pra data da baixa
  original — no dia da execucao, ~255 croissants negativos zerariam a
  demanda do (item, dia) em `prever_demanda` (clamp em 0) e poluiriam a
  media do dia-da-semana por ~8 semanas; re-datado, venda e estorno se
  anulam no mesmo dia historico (venda de evento nao e demanda da loja);
  (3) GUARDAS nos fluxos pos-acerto em `loja_pagamento._acertado_no_
  despacho`: cancelamento NAO re-credita a loja (a fase 1 re-varreria os
  movs originais = dobro; frases ja protegidas por `estornado_em`) e
  `reduzir_item_pedido_pago` RECUSA antes do refund; (4) previa exclui
  movs com tag `(fracao)`/`(fator` (a fase 1 do estorno os pula — dry-run
  nao promete mais do que o executar devolve); (5) falta na industria
  persiste como mov `saida_site_direto_sem_estoque` (o JSON da resposta se
  perde, o ledger nao); (6) plano com `credito_por_loja` (dimensao de
  loja — retirada credita a loja ESCOLHIDA, visivel no dry-run) +
  `pedidos_retirada` + `nada_a_fazer` + `credito_aplicado_movs` (contagem
  de movimentos, nome honesto).
- NAO rodar antes do despacho fisico; NAO aplicar conferencia na loja de
  origem nem na industria antes do acerto (corrigiria 2x). RETIRADAS
  entram no acerto (o dono confirma no dry-run pelo `pedidos_retirada` —
  se alguma saiu da prateleira da loja, acertar so os demais e caso
  manual).
Testes: `tests/test_acerto_despacho.py` (18 casos). PENDENTE (decisao
separada, proxima data especial): flag em `LojaDataEspecial` tipo "sai da
industria" roteando a baixa do site pra EstoqueProducao ja no pagamento.
Advisory lock 7757 RESERVADO pro acerto de despacho.

### Horario de entrega ESPECIAL por data (27/07/2026)

Pedido do dono: "no dia 09/08 tenha somente uma janela de horario para
entrega: das 06:00 as 10:00. E dia dos pais". Escolhas dele
(AskUserQuestion): express **BLOQUEADO** no dia, **retirada TAMBEM**
restrita a mesma faixa, e a data vira **CADASTRO numa tela** (nao constante
no codigo) pra ele resolver Natal/Dia das Maes sem deploy.

- **Modelo** `LojaDataEspecial` (data unica, `janelas` uma por linha,
  `express_bloqueado`, `rotulo`). **Tabela NOVA via `db.create_all`** — o
  procedimento de 2 commits vale pra COLUNA nova, nao pra tabela.
- **Contrato que nao pode regredir**: `janelas_do_dia(data)` devolve
  `(tem_regra, janelas)`. `(False, [])` = dia normal; `(True, [...])` = usa
  EXATAMENTE essas; `(True, [])` = **dia FECHADO**. Lista vazia NUNCA pode
  cair no horario normal — viraria "fechado" em "aberto o dia inteiro". Por
  isso a tupla: `[]` nao serve de sentinela. O JS espelha com
  `hasOwnProperty`, nao com `||`.
- **Ponto unico**: `loja_checkout.janelas_disponiveis` — o site oferece, o
  `criar_pedido` valida e o endpoint da divulgacao consultam TODOS por ela.
  `express_disponivel` consulta a data (por isso POST forjado tambem bate na
  trava) e `datas_disponiveis` tira o dia fechado do calendario.
  `janelas_do_modo` foi **REMOVIDA**: era codigo morto sem chamador que
  devolvia a lista global ignorando a data — quem a usasse furaria o dia
  especial em silencio.
- **O front NAO pergunta janela por data** (o seletor e montado no cliente a
  partir da lista global). Por isso `_ctx_checkout` manda
  `janelas_por_data` (`janelas_especiais_do_periodo`); sem ele o site
  mostraria 08:00-18:00 no 09/08 e so o POST recusaria — anulando a feature
  na pratica. O mapa cobre o INTERVALO inteiro (nao so as datas validas):
  o `<input type=date>` e min/max contiguo, entao dia fechado continua
  clicavel e precisa aparecer com `[]`.
- **Corte da 1a janela por distancia NAO se aplica** a dia especial (server
  e JS): `JANELAS_CORTADAS_LONGE` e keyed na string '08:00–09:00' e cortar a
  janela unica zeraria o dia pra quem mora longe.
- **Traco**: o dono digita hifen no celular; `normalizar_janela` converte pra
  EN-DASH (o resto do sistema compara janela por string).
- **Seed** do 09/08 em `migrations_legacy`, marcado por AppConfig: roda UMA
  vez e **nao ressuscita** se o dono apagar/alterar — cadastro do dono manda
  sobre seed. Pulado sob `PYTEST_RUNNING` (senao a suite ficaria
  date-dependent: a agenda de 14 dias cobre 09/08).
- **Bot de atendimento**: `chatbot._horarios_especiais_texto` injeta as datas
  dos proximos 14 dias no system prompt — o prompt crava "todos os dias das
  8h as 18h" (`chatbot_prompt.py:339,507,520`) e mentiria no dia de maior
  movimento. Sem data cadastrada devolve '' (nao infla token nem mexe no
  cache).
- Consertados no caminho (defeitos ANTIGOS): `pdf._latin1` imprimia
  `08:00?09:00` no papel do motorista (latin-1 nao conhece en-dash) e a
  mensagem de erro fatiava a janela com HIFEN (`split('-')`) numa string com
  en-dash.
- Tela: `/admin/loja-online/horarios-especiais` (owner) + card no painel da
  loja online. Manual de operacao registrado.
- **Bloqueio de ITENS por data (07/08/2026, caso "Caixa de Mini vendida pro
  Dia dos Pais" — dono: "os clientes nao poderiam comprar os minis para o
  dia 9")**: `LojaDataEspecial.bloquear_itens` (TEXT, procedimento de 2
  commits — ALTER confirmado pela sonda ?colunas= antes do modelo). Uma
  REGRA por linha: nome de CATEGORIA ou de ITEM do catalogo (comparacao
  sem acento/caixa — `_norm_regra`). `loja_data_especial.itens_bloqueados
  (data, itens)` e chamado no `criar_pedido` DEPOIS do bloco de esgotados;
  a recusa cita o rotulo da data ("cardapio especial"), diferente da msg de
  esgotado (curadoria != falta de estoque). FAIL-OPEN deliberado (erro na
  consulta = nao barra — mesmo contrato do regra_do_dia: problema aqui
  nunca derruba o checkout). `definir(bloquear_itens=None)` NAO mexe no
  gravado (compat com seed); `''` limpa de proposito — a tela SEMPRE manda
  o campo e o botao Editar pre-carrega (`data-bloqueios`), senao corrigir o
  rotulo apagaria os bloqueios. LIMITACAO CONHECIDA: a VITRINE nao esconde
  o item por data (o cliente so descobre no checkout) — mostrar aviso na
  pagina do produto e melhoria separada. Testes: secao "Bloqueio de ITENS"
  em `tests/test_horario_especial.py`.
- **POS-REVISAO (fixados)**: (1) **FECHAR O DIA virou CHECKBOX EXPLICITO** —
  era "deixe o campo em branco", e como o form nasce vazio e `definir` e
  upsert, reabrir a tela so pra corrigir o rotulo FECHARIA o site no Dia dos
  Pais sem confirmacao (pior falha possivel aqui). Textarea vazio sem o
  checkbox agora RECUSA; e a lista ganhou botao **Editar** que carrega os
  valores atuais no form. (2) dia FECHADO bloqueia express SEMPRE, mesmo com
  a caixa desmarcada — o express nao olha a lista de janelas, entao o
  contrato "a data some do site" nao valia. (3) `regra_do_dia` faz
  **rollback** no except (no Postgres a transacao abortada mataria a request
  inteira — a promessa "pior caso vira o horario de sempre" so vale com
  isso). (4) `regras_do_periodo` resolve as ~15 datas do render em UMA query
  (era 1 SELECT por data, e 15 `logger.exception` com banco intermitente).
  (5) `_sem_janelas_passadas` tolera janela ilegivel (a coluna e texto; uma
  linha escrita por fora com '6:00-10:00' fazia `int('6:')` estourar DENTRO
  do render = site em 500). (6) a tela AVISA quando ha **pedido ja pago**
  pra aquela data com horario que a regra nova nao oferece mais (a agenda e
  de 14 dias — da pra ter venda anterior ao cadastro; o sistema NAO muda
  pedido feito). (7) teste do payload deixou de cravar 09/08 (quebraria a
  suite a partir de 10/08/2026 e, com Wait-for-CI, travaria TODO deploy).
- **PENDENCIAS ACEITAS (baixa, decisao separada)**: `divulgacao.
  criar_divulgacao` so exige janela nao-vazia (pre-existente; o select da
  tela ja herda a regra); a pagina do PRODUTO nao olha dia fechado no
  seletor de data (o checkout barra); `LojaDataEspecial` fora de
  `AUDITED_MODELS` e `criado_por_id` nao atualiza na edicao; `definir` sem
  try/except de IntegrityError (um dono so).
- Testes: `tests/test_horario_especial.py` (45 casos). Validado a 390px com
  Playwright: 11 checks no checkout do cliente + 9 na tela do dono.

### Vitrine anuncia a PROXIMA DATA, nao "esgotado hoje" (27/07/2026)

Pedido do dono: "quando o item nao tem disponivel para hoje colocar o dia
que ele vai estar disponivel". O cliente batia numa negativa seca ("ESGOTADO
HOJE") sem saber quando voltava — motivo de abandono, nao de informacao.

- `anotar_esgotado` passou a expor `proxima_data` (**ISO string**),
  `proxima_data_label` ("amanha" / "sexta, 31/07" / "07/08") e
  `proxima_data_curta` (so a data, pro selo sobre a foto). Sai DE GRACA: o
  loop dos 14 dias ja parava no primeiro dia com saldo, so nao guardava
  qual era. ISO e nao `date` de proposito — o mesmo dict vai pro
  `bot_tools` e pra JSON, onde `date` nao serializa (ha teste travando).
- So anuncia quando a data e util: item vendavel HOJE nao ganha rotulo, e
  **esgotado duro nao promete data nenhuma** (a etiqueta vermelha continua
  sendo a verdade). Os textos antigos ficaram de fallback.
- Rotulo por `loja_catalogo.rotulo_data_disponivel(data, referencia)` —
  funcao PURA (recebe a referencia, testavel sem mexer no relogio). Dia da
  semana so ate 7 dias: alem disso "qual sexta?" confunde mais que ajuda.
- A rota do produto (`loja.produto`) NAO recalcula mais a proxima data com
  loop proprio — le `item['proxima_data']`. Eram duas contas do mesmo fato
  e podiam divergir: card anunciando um dia e o seletor abrindo em outro.
- CSS: `.selo-esgotado-hoje` ganhou `max-width`/`line-height` — o texto novo
  e mais largo e o selo e absolute sobre a foto (a 390px o card tem ~175px).
  Validado a 390px com Playwright (11 checks, incl. "selo nao vaza da foto"
  e "data do aviso == data pre-selecionada no seletor").
- Testes: secao "Etapa 4" de `tests/test_loja_plano_dia.py`.

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

**FRETE na venda B2B (20/07/2026, pedido do dono via Bruno)**:
`VendaB2B.frete_valor` Numeric(10,2) NOT NULL DEFAULT 0 (procedimento de
2 commits — ALTER confirmado por /api/claude/deploy antes do modelo).
Regras:
- Frete SOMA no `valor_total` (criar_venda/editar_venda via
  `_normalizar_frete`; negativo = ValueError) → parcela unica, boleto
  Sicredi e fatura mensal (que soma valor_total) herdam SOZINHOS.
- NF do Tiny: `tiny_nf_b2b._nota_payload(frete_valor=...)` manda o valor
  no campo `valor_frete` (mesmo padrao da NF do site — o Tiny fecha o
  total da nota = Σ itens + valor_frete, entao NF e boleto saem no MESMO
  valor). NF consolidada da fatura mensal manda a SOMA dos fretes das
  vendas do periodo (fecha exato com fat.valor_total).
- Form web: campo "Frete da entrega (R$)" no cabecalho de
  `venda_nova.html` (parse FORA de `campos` em `_parse_venda_form` — o
  caminho de venda PAGA usa editar_cabecalho(**campos) e frete fica
  TRAVADO junto com itens/parcelas, JS tambem trava). Detalhe da venda
  mostra Itens/Frete/Total quando frete > 0.
- ORCAMENTO: `validar_para_aprovacao` NAO exige mais frete zerado (a
  regra de 07/07 "embuta o frete" existia so porque a venda nao tinha o
  campo); `_converter_em_venda` passa `orc.frete_valor` pra venda e o
  "→ Virar venda" manual seeda o campo (`frete_pre`). DESCONTO em R$
  continua exigindo embutir (esse segue sem campo na venda).
- Copilot `criar_venda_b2b`: param opcional `frete_valor` (schema +
  executor + enricher/preview do Slack mostram "inclui frete").
POS-REVISAO (fixados): `editar_venda` agora tem o MESMO guard do
`excluir_venda` pra boleto — titulo que JA FOI AO BANCO recusa a edicao
e cobranca PENDENTE e apagada junto das parcelas (antes o delete da
parcela NULLificava o FK e o boleto orfao seguia vivo com o valor VELHO:
liquidacao silenciava e a parcela nova virava candidata a um 2º boleto —
reproduzido pela revisao; classe pre-existente, agravada pelo frete);
frete do form parseia com `parse_float_br` (invalido = flash + nada
criado, nunca R$ 0 calado) e inf/nan de POST forjado vira ValueError
tratado; erro no POST de criar preserva `?orcamento=` no redirect;
seed manual de orcamento com DESCONTO avisa que o desconto NAO entra
(a venda sairia MAIOR); parcela de venda CANCELADA nao vira boleto
(`gerar_da_parcela` recusa). PENDENCIAS DOCUMENTADAS (decisao separada):
soma de parcelas EXPLICITAS segue sem validacao contra o total
(pre-existente; com frete o descasamento fica mais provavel — validar
quebraria a "excecao negociada"?); `frete_por_conta='R'` com
valor_frete>0 e o mesmo padrao do site mas vale conferir com o contador
(modalidade CIF vs frete destacado); orcamentos ANTIGOS (pre-20/07) que
embutiram o frete nos precos E mantiveram frete_valor>0 cobrariam o
frete 2x ao aprovar — conferir os enviados antes de aprovar; venda
FATURADA ainda mostra "Emitir NF" (NF da venda + NF da fatura duplicaria
itens e frete na SEFAZ — pre-existente); editar_venda(frete_valor=0)
como default ZERA frete existente se um caller futuro omitir o param.
Testes: `tests/test_b2b_frete.py` (14 casos) + regra nova em
`test_orcamento_aprova_vira_venda.py`.

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
**Disponibilidade e POR DATA (04/08/2026, caso Giovana conv 1134)**: na
vespera do Dia dos Pais o plano-do-dia de 09/08 so vendia cestas; o bot
negou croissant/cinnamon/pao frances PRA 09/08 (certo) e o vigia — que so
via a disponibilidade GERAL — acusou "erro real" (falso ALTA; o dono pegou:
"o vigia nao entra no espirito do bot"). Fix em 3 camadas:
`loja_plano_dia.saldos_no_periodo(di, df)` (1 query),
`bot_tools.catalogo_disponibilidade()` agora devolve `indisponivel_em`
(datas dd/mm da janela de 14d em que o plano ZEROU o item; sob encomenda
nunca entra), o resumo do vigia imprime "INDISPONIVEL para entrega em:
DD/MM" (dedup por nome INTERSECTA as datas — homonimo livre libera o nome)
e o PROMPT_VIGIA manda NAO contradizer o bot quando ele nega o item pra
data listada. O `consultar_produtos` do BOT tambem expoe `indisponivel_em`
no match focado (antes o bot acertava a data por eco do cliente, nao por
dado). Testes: `tests/test_vigia_disponibilidade_por_data.py` (8 casos).

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

## Presente no site — telefone do COMPRADOR × de quem RECEBE (13/07/2026)

Caso real (dono): cliente comprou cesta de presente e pos o telefone da
ESPOSA (destinataria) no campo principal do checkout; a padaria ligou pra
tirar duvida e ESTRAGOU A SURPRESA. O modelo `PedidoOnline` ja separava
`telefone_cliente` (comprador/pagador, recebe contato comercial) de
`telefone_destinatario` (quem recebe; NULL fora de presente) — o furo era de
UI, em duas camadas:

- **Checkout** (`app/templates/loja/checkout.html`): o campo `telefone` era
  so "Telefone (WhatsApp)", unico do bloco do comprador sem dizer "de quem".
  Agora "**Seu telefone (WhatsApp)**" + help ("a gente fala com VOCE; e
  presente? poe o SEU numero, nao o de quem recebe"); o campo do destinatario
  virou "Telefone de quem vai receber" + help ("so pro entregador ligar").
- **Painel de entregas** (`entregas/routes.py::_serializar_pedido_online` +
  `painel_pedidos.html` + `imprimir.html`): o card mostrava
  `telefone_destinatario or telefone_cliente` como um "📞" generico — ler e
  ligar caia em quem recebe. Agora serializa `telefone_comprador`
  (=telefone_cliente, duvidas) e `e_presente` (destinatario OU cartinha)
  alem de `telefone` (=entrega, motoboy). Num presente o card separa
  "📞 Entrega (quem recebe)" de "☎️ Comprador (duvidas)", poe badge
  "🎁 PRESENTE" e o aviso "nao ligue pra quem recebe". O comprador e SEMPRE
  rotulado — sem telefone dele, o card mostra "sem telefone informado" em vez
  de repetir o numero de entrega cru (senao o operador ligava pra quem recebe
  achando ser o comprador — pego em revisao). O PDF oficial (`pdf.py::
  gerar_pedidos_pdf`, o papel que vai com o motoboy) marca "DESTINATARIO -
  PRESENTE", rotula "Tel. entrega" e imprime "Presente - nao comentar o
  conteudo com quem recebe" — protege a surpresa na entrega.

**Operacao ja estava certa por baixo (NAO regredir)**: motoboy/Lalamove usa
`telefone_destinatario` (porta), o botao "Chamar cliente" e Pagar.me/NF usam
`telefone_cliente` (comprador). Login/identidade e por E-MAIL, nao telefone —
mudar o ROTULO do campo e seguro. Testes: `tests/test_pedido_presente_telefone.py`.

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
  - **Ver os dados coletados**: `/admin/clientes` (admin+owner, link na área
    Administração) — lista os `Cliente` do varejo (site + portal Wi-Fi) com
    busca, filtro de aniversariantes do mês e export XLSX pra campanhas.
    Rota `main.clientes` + `main.clientes_xlsx`. Testes:
    `tests/test_clientes_admin.py`.

## Bot de atendimento — hardening 02/07/2026 (4 pacotes)

Pesquisa de melhorias no bot/vigia/auditor aprovada pelo dono virou 4
pacotes, todos implementados. Testes: `tests/test_bot_melhorias_0702.py`.

**26/07/2026 (auditor: caso Gabriela conv 918 + turno vazio)** — dois
defeitos reais, um deles o motivo de handoff MAIS FREQUENTE do periodo.
Diagnostico feito com a sonda `/api/claude/vigia-vereditos` (o relatorio do
auditor nao traz `conv_id`; SEMPRE puxar a conversa real antes de agir — o
enquadramento do auditor estava errado em 2 dos 3 achados).

- **Turno VAZIO do modelo nao e falha — quase sempre e fechamento.** O
  prompt manda "NAO responda nada" no fechamento; o modelo obedece metade
  (silencio) e esquece a outra (chamar `encerrar_conversa`), devolvendo
  turno sem tool e sem texto. O codigo tratava isso como falha e transferia
  com motivo `'resposta vazia'`: **3 dos 16 handoffs de 12-26/07** sairam
  assim (conv 842 "Obrigada. Esclareceu", 897 "Nao, muito obrigada !", 918),
  entupindo a fila humana com fechamento banal e ainda contando como
  "handoff preguicoso" na metrica do auditor (tools vazias) — era dai que
  vinha o "preguicoso 1/2" do relatorio. O ramo agora DISCRIMINA 3 casos:
  (a) fechamento COM reclamacao (`_SINAIS_RECLAMACAO`) -> handoff com
  mensagem DE VERDADE (`_TEXTO_VAZIO_RECLAMACAO`), porque silenciar venda
  perdida e o pior desfecho; (b) sem pendencia -> `_resp_encerrar`, o
  SILENCIO que o dono decidiu em 16/06 (mesma saida da Camada 1, que so nao
  pegou porque `_e_fechamento` e ancorado nas duas pontas e nao tolera texto
  extra); (c) bot com PERGUNTA pendente -> `FALLBACK_TEXTO` (regra P1,
  cliente nunca no vacuo). NAO alargar `_e_fechamento` pra pegar esses
  casos: ele silenciaria RECLAMACAO (o caso 918 termina em "Obrigada").
- **Atraso de pedido de APP (Rappi/iFood/99Food) transfere DIRETO.** Caso
  Gabriela: "pedido do Rappi previsto 17:20 e ate agora nada. **O motorista
  ja esta ai**" — o entregador estava NO BALCAO (vigia: "3 motoboys foram
  embora"), ou seja o gargalo era NOSSO. O bot mandou pro suporte do Rappi
  (lugar errado) e ninguem na padaria soube; a cliente cancelou 1h depois.
  Duas causas: (1) NENHUMA tool consulta pedido de marketplace
  (`consultar_pedido` so ve `PedidoOnline`), entao o enforcement que exige
  "consulte antes de transferir" era beco sem saida — o caminho barato pro
  modelo virava o texto generico "veja no app"; (2) `_HANDOFF_EXCECAO` nao
  cobria atraso, embora o VIGIA ja tratasse atraso como handoff LEGITIMO
  (`_SINAIS_RECLAMACAO`) — duas implementacoes do mesmo conceito divergindo.
  Agora: excecao cobre `atras*`/rappi/ifood/99food/marketplace + secao no
  prompt (bullet em "ANTES DE TRANSFERIR") mandando transferir SEM pedir
  numero de pedido (pedido de app nao tem numero nosso) e distinguindo
  "entregador ja esta aqui" (gargalo nosso, aciona humano) de "pedido ja
  saiu" (ai sim orienta o app).
- **Dois defeitos pre-existentes achados no caminho, corrigidos**: o radical
  `alerg` NUNCA casou "alérgico"/"alérgica" (acento) — a excecao de MAIOR
  risco (saude) so valia se o modelo escrevesse "alergia"; e `pessoa\w*`
  casava **"pessoas"**, entao um motivo de VENDA ("cesta para 10 pessoas")
  anulava o enforcement inteiro. `_SINAIS_RECLAMACAO` tambem nao reconhecia
  "nao chegava/chegaram" (so "chegou") nem "cancelei/cancelando" (so
  "cancelar meu pedido") — por isso a venda perdida da 918 era lida como
  fechamento banal.
- **A sugestao do auditor foi PARCIALMENTE recusada** (ele e IA, o dono
  manda): ele pediu "resposta padrao de empatia" no fechamento, o que
  contraria a decisao de 16/06+21/07 (fechamento puro = SILENCIO). Empatia
  entra SO no ramo com reclamacao. O 3o achado dele ("pedido entregue mas
  nao recebido", R$309) foi confirmado SEM defeito de codigo: o bot usou
  `consultar_pedido` e transferiu com motivo completo — e acompanhamento
  operacional, nao software.
- Testes: `tests/test_bot_resposta_vazia.py` (10 casos).

**21/07/2026 (caso Daiane Food Center, auditor)** — fechamento puro NUNCA
vira handoff. A fornecedora agradeceu ("Muito Obrigada🙏") depois de o bot ja
ter respondido e o bot respondeu "Já te passo para um atendente" — handoff
preguicoso. A secao FECHAMENTO do prompt (decisao do dono 16/06: agradecimento
puro = `encerrar_conversa` em SILENCIO) existia, mas o modelo a ignorou. Fix em
DUAS camadas + reforco no prompt (`tests/test_chatbot_encerrar_e_ig_mention.py`,
grupo "Fix 3"):
- **Camada 1 (deterministica, ANTES do modelo)** em `chatbot.responder`: se
  `_e_fechamento(msg)` (reusa o detector do vigia) e o bot NAO deixou pergunta
  pendente (`_bot_aguarda_resposta` = ultima fala do bot termina em '?'),
  encerra em silencio sem nem chamar o Claude. A trava do '?' evita encerrar
  quando "ok/sim/isso" e resposta a "confirma o pedido?" (= "sim, quero").
- **Camada 2 (enforcement)**: quando o bot fez pergunta (Camada 1 defere) e o
  modelo mesmo assim tenta handoff preguicoso num fechamento, a recusa mandada
  no tool_result orienta `encerrar_conversa` (nao "consulte antes" — nao ha o
  que consultar num "obrigada"). O modelo encerra na volta seguinte.
- **Prompt**: "🚫 obrigada/valeu/ok NAO e pedido de atendente. NUNCA chame
  transferir_para_humano num fechamento — a ferramenta certa e
  encerrar_conversa." Mantida a decisao do dono de SILENCIO (nao "De nada!").

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

## Vigia de venda SEM itens no PDV (18/07/2026)

Pedido do dono ("preciso de um alerta imediato dessas vendas") no caso
Nebraska 17/07: 23 cobrancas "PDV Facil" com valor e ZERO produtos
(R$ 7.028,50, TODAS sem NF e sem forma de pagamento, um unico caixa,
15h-19h49). O painel da Seru nao as mostra (relatorio deles e por
produto/nota); aqui so aparecem no `faturamento_pedidos` — nao baixam
estoque nem entram na previsao.

- **Servico**: `app/services/venda_sem_item_vigia.py`. Roda a cada ciclo
  do sync Seru (15min), DENTRO do advisory lock do `_run_sync` (execucao
  unica entre workers — sem alerta duplicado). Janela ontem+hoje (mesma da
  captura). Cobranca suspeita = nao cancelada, total > piso, zero itens
  nao-cancelados NA LISTAGEM.
- **RE-VERIFICACAO no DETALHE (21/07/2026, caso R$155 O Pao Padaria)**: a
  LISTAGEM da Seru (`listar_pedidos_completo`) ATRASA pra cobranca
  recem-criada — ela aparece sem itens E sem NF na lista mesmo JA tendo
  NFC-e autorizada (a R$155 foi criada 14:58, NFC-e as 15:06, e as 15:31 a
  lista ainda a mostrava vazia -> FALSO POSITIVO: cupom fiscal com 7 itens
  + cartao). Fix: a lista virou so PRE-FILTRO barato; toda suspeita e
  re-conferida no `seru.detalhes_pedido` (GET /orders/{id}, fonte
  autoritativa) antes de alertar — venda REAL se o detalhe tem item
  nao-cancelado OU NFC-e fiscal (`_nf_autorizada`). Detalhe
  indisponivel = NAO alerta nesse ciclo (retenta; id nao entra no dedup).
  **CONTINGENCIA conta como NF (23/07/2026, caso Nebraska cod 19989588)**:
  `_nf_autorizada` passou a aceitar `_NF_STATUS_FISCAL = {'authorized',
  'contingency'}` — NFC-e em contingencia e emitida OFFLINE quando SEFAZ/
  internet cai (DANFE impressa e entregue, transmitida depois), com produtos;
  antes so `authorized` contava e uma venda real (cafe + cookie, NFC-e nº
  2360 em contingencia) virava falso positivo. So falta de nota MESMA
  (taxInvoice None ou cancelado/negado) segue suspeita. A mesma checagem
  alimenta a coluna "tem NFC-e" do drill-down da home (briefing_dono importa
  `_nf_autorizada`). NAO confundir com o OUTRO padrao real que segue
  alertando: cobranca de VALOR AVULSO (item placeholder sem nome/qtd, que o
  `extrair_itens` descarta) paga no cartao SEM NF nenhuma (as 4 de 23/07 —
  Anesio caixa 683867, O Pao Padaria caixa 683812).
  Decisao do dono: SO reverificar, sem carencia — alerta segue imediato
  pras cobrancas que o detalhe confirma fantasma (ex. cod 19875201: 13
  itens TODOS cancelados, sem NF, pagamento vazio). Sonda usada no
  diagnostico: `/api/claude/vendas-snapshot?detalhe=<id>`.
- **Dedup POR PEDIDO** em AppConfig (`venda_sem_item_alertados` = JSON
  {data: [ids]}, podado pra janela): cada cobranca alerta UMA vez; varias
  novas no ciclo = UMA mensagem WhatsApp (por company, com hora, valor,
  NF tem/nao-tem e caixa, cap 8 linhas + "e mais N"). Envio falho NAO
  marca os ids (retenta no proximo ciclo — perder alerta de possivel
  fraude e pior que duplicar). Sem numero do dono configurado tambem nao
  marca (quando configurar, alerta tudo).
- **ANTI-FLOOD (dono 18/07: "cuidado pra nao bloquear a conta")**: 1a
  cobranca alerta NA HORA; as seguintes acumulam (ids nao marcados) e saem
  juntas na proxima janela — cooldown `VENDA_SEM_ITEM_COOLDOWN_MIN`
  (default 60min; 0 = por ciclo) + teto `VENDA_SEM_ITEM_MAX_MSGS_DIA`
  (default 6/dia). SEM `critico=True` de proposito — respeita tambem o
  teto/hora global do zapi (segurada = ok False = retenta). Estado v2:
  {'ids': {data: [ids]}, 'ultimo_envio', 'envios': {data: n}} (formato
  antigo migra sozinho).
- **Env**: `VENDA_SEM_ITEM_VIGIA=0` (kill-switch),
  `VENDA_SEM_ITEM_MIN_VALOR` (piso em R$ por cobranca, default 0).
- Sob demanda: `GET /admin/vigia-venda-sem-item` (owner; dry-run lista
  cobrancas+estado SEM WhatsApp; `?alertar=1` roda o fluxo). Sonda
  externa: `/api/claude/vendas-snapshot?pedidos=1`. Manual atualizado.
- **Pos-revisao (fixados)**: rollback no except da captura do cron (sem
  ele, um `capturar_periodo` que falhasse no MEIO deixava DELETEs
  pendentes que o commit seguinte da mesma sessao persistia — dias
  sumiam dos relatorios por 15min); sessao envenenada nao cega o vigia
  (rollback + erro visivel, padrao uso_ia_vigia); UM pedido com `total`
  malformado nao mata a varredura (try/except por pedido); rota
  `?alertar=1` pega o try-lock do sync (corrida rota×cron nao duplica
  WhatsApp); dinheiro em formato pt-BR via `app.utils.fmt_brl` (novo,
  centralizado); NF cancelada/negada conta como SEM NF; acumulado da
  mensagem cobre a janela ontem+hoje inteira. Limitacao ACEITA: janela
  por createdAt — pedido antigo esvaziado hoje nao entra.
- Testes: `tests/test_venda_sem_item_vigia.py` (16 casos; Seru e Z-API
  sempre mockadas).
- **Card "Por loja (PDV)" com rodape (18/07/2026, "rodape para eu
  investigar")**: a captura grava a dimensao nova `sem_itens` no
  `VendaSeruDiaBreakdown` (chave '' = VALOR e chave 'n' = CONTAGEM das
  cobrancas so-valor por dia/company — SEM mudanca de schema, a tabela foi
  feita pra eixos novos); a LINHA da loja no card mostra so venda COM
  produto (`por_loja` continua o total cheio por compat — o FRONT subtrai
  `por_loja_sem_itens`) e o rodape "⚠ Cobranças sem produto (nao somam
  acima — investigar)" so aparece quando ha valor. Nos 3 modos (banco
  cheio, banco filtrado por loja, ao vivo). Dia capturado ANTES da
  dimensao existir nao tem a linha → split 0 e o card mostra o total
  cheio como era (recapturar via cron ontem+hoje ou "Aquecer historico"
  povoa). **RESUMO tambem desconta (dono 18/07, "contar somente o valor
  das vendas com itens")**: Faturamento/Pedidos/Ticket medio do /pdv/
  descontam valor E contagem das cobrancas sem produto, com aviso amarelo
  dizendo o que ficou fora; dia antigo sem a chave 'n' desconta so o
  valor. Testes: secao sem_itens em `tests/test_vendas_diarias.py`.
- **DELIVERY sem itens e OUTRA classe (dono 18/07, caso 99Food Anesio
  R$81,38)**: pedido de canal de delivery (constante
  `SEM_ITENS_CANAIS_DELIVERY` em `app/constants.py` — 99food/ifood/rappi)
  chega SEM itens por natureza da integracao mas e venda REAL: CONTA no
  faturamento/pedidos/ticket, aparece em aviso informativo proprio
  ("conta no faturamento; nao baixa estoque") e em secao propria do
  rodape (🛵). So avulsa (pdv-facil/desconhecido) fica fora do resumo e
  alerta o vigia (`venda_sem_item_vigia.canais_ignorados`, env
  `VENDA_SEM_ITEM_CANAIS_IGNORADOS` substitui a lista). A dimensao
  `sem_itens` agora e POR CANAL (chave '<tag>' valor, '<tag>:n' contagem;
  formato antigo ''/n cai no bucket avulsa). O alerta do vigia mostra o
  CANAL de cada cobranca. **Cancelamento canonico**:
  `seru.pedido_cancelado` (canceledAt OU status=='canceled' — caso real
  18/07, cobranca cancelada sem canceledAt contava como venda) usado na
  camada de RELATORIO/vigia; o `seru_sync` (estoque/estorno) segue keyed
  em canceledAt DE PROPOSITO — mudar o gatilho de estorno e decisao
  separada, PENDENTE de auditoria propria. PENDENTE tambem: dono quer
  pedido 99Food com vinculo de produto e BAIXA DE ESTOQUE. DESCOBERTA
  18/07 (sonda `?detalhe=<id>` no vendas-snapshot): o GET /orders/{id}
  NAO traz os produtos (item so com taxInfo vazio), MAS o
  `taxInvoice.xmlUrl` (S3 da Seru) entrega o XML da NFC-e com os
  produtos REAIS (<det><prod><xProd>/qCom/vProd — provado no pedido
  3377f6c3/NF 724: 6x Pao Frances + 1x Croissant Nutella Com Morango +
  1x Brioche, nomes iguais aos do SeruProdutoMap). **CONSTRUIDO
  19/07/2026 (GO do dono)**: `seru.itens_da_nf(pedido)` baixa o XML do
  `taxInvoice.xmlUrl` (cap 3MB, parser ElementTree namespace-agnostico)
  e devolve itens na MESMA forma de `extrair_itens`; o
  `seru_sync.processar_pedidos` enriquece pedido NOVO sem itens antes da
  baixa — mesmos mapeamentos/motor/idempotencia de sempre. Contratos:
  sem NF nenhuma = [] (processado com 0 itens, status quo); NF
  cancelada/negada = []; download/parse FALHOU = None -> pedido NAO
  marca processado (`pedidos_aguardando_nf` no stats, retenta no
  proximo ciclo — padrao do aguardando-loja). Item da NF sem mapa vira
  pendente em /pdv/mapeamentos como qualquer venda. Pedido COM itens
  nunca consulta a NF (zero custo extra). O relatorio de PRODUTOS
  (captura vendas_diarias) segue SEM os itens do delivery (bucket 🛵) —
  enriquecer a captura re-baixaria o XML a cada ciclo de 15min;
  previsao motor=vendas JA enxerga (le MovEstoqueLoja, que a baixa
  cria). **Pos-revisao (fixados)**: XMLs pre-buscados em `nf_cache`
  ANTES do `serializar_lojas` (I/O de rede nao segura os advisory
  locks de todas as lojas); pedido NOVO cancelado por STATUS (sem
  canceledAt) nao baixa pela NF (`seru.pedido_cancelado` no ramo de
  pedido novo — sem o guard, baixava venda cancelada e o estorno,
  keyed em canceledAt, nunca disparava); `qCom` 0 na NF e pulado
  (bonificacao nao vira baixa de 1); parse do XML em BYTES (expat
  respeita o encoding declarado — decode com 'replace' corrompia
  acento); `LOCK_KEY_REPROCESSO` movido 7749→7752 (colidia com o lock
  do GOOGLE_REVIEWS — reprocesso e reviews podiam se excluir
  mutuamente em silencio). PENDENCIAS DOCUMENTADAS (nao bloqueiam,
  decisao separada): estorno de pedido JA processado segue keyed so em
  canceledAt (cancelamento por status depois de processado nao
  estorna); qCom fracionario e arredondado no motor (pre-existente);
  NF quebrada que envelhece pra fora da janela de sync some sem
  alarme; o PRIMEIRO reprocesso retroativo apos o deploy baixa
  pedidos de delivery ANTIGOS (ate 30d) de uma vez — rajada esperada,
  avisar o dono. Testes: `tests/test_seru_nf_itens.py` (9 casos).

## Vigia de ESTORNO PENDENTE — cancelada que nao devolveu estoque (26/07/2026)

Caso real: 4 cobrancas canceladas entre 22 e 24/07/2026 tinham baixado
estoque (7 itens) e NUNCA devolveram; so apareceram numa auditoria manual.
Causa = a pendencia ja documentada acima: o estorno de pedido JA PROCESSADO
(`seru_sync.processar_pedidos`) e keyed SO em `canceledAt`, e essas foram
canceladas so pelo `status` — a condicao nunca fecha.

**DECISAO DO DONO (26/07/2026)**: "esses 4 deixa pra la, vejo os proximos" e,
avisado de que os proximos passariam batidos igual, escolheu **ALERTAR** em
vez de mexer no gatilho. Ou seja: **o gatilho do estorno NAO mudou** (segue
`canceledAt`), o vigia so torna visivel o que era silencioso. NAO fazer o
vigia estornar — estoque so muda por gesto do dono.

- **Servico** `app/services/estorno_pendente_vigia.py`. Regra CANONICA
  `e_estorno_pendente(pedido, reg)` = processado, nao estornado,
  `n_itens_baixados > 0`, cancelado por `seru.pedido_cancelado` MAS **sem**
  `canceledAt`. Usada nos DOIS caminhos (sync e tela) pra nunca divergirem.
- **Deteccao sem custo**: mora no proprio `processar_pedidos` (que ja tem os
  pedidos da API e o registro na mao) e sai em `stats['estornos_pendentes']`
  — zero request extra a Seru. A tela sob demanda usa `detectar(di, df)`,
  que refaz a leitura read-only.
- **Alerta**: `alertar(pendentes)` roda a cada ciclo do sync (15min) DENTRO
  do advisory lock do `_run_sync` (execucao unica entre workers). Dedup POR
  PEDIDO em AppConfig (`estorno_pendente_alertados`), envio falho NAO marca
  os ids (retenta). Anti-flood igual ao `venda_sem_item_vigia`:
  `ESTORNO_PENDENTE_COOLDOWN_MIN` (60) + `ESTORNO_PENDENTE_MAX_MSGS_DIA` (4);
  kill-switch `ESTORNO_PENDENTE_VIGIA=0`.
- A mensagem diz **o que saiu do estoque** (`itens_baixados(pedido_id)` le
  os `MovEstoqueLoja` pela MESMA referencia `Seru #<id>`, incluindo os
  sufixos de cesta/fracao) — sem isso o dono saberia que ha um rombo mas nao
  o que devolver. Detalhe e BONUS: falha nele nunca derruba o alerta.
- Sob demanda: `GET /admin/vigia-estorno-pendente` (owner; dry-run lista
  pendentes+estado sem WhatsApp, `?alertar=1` roda o fluxo pegando o
  try-lock do sync — corrida rota×cron nao duplica WhatsApp nem apaga ids).
- **POS-REVISAO (fixados)**: (1) CRITICO — `company` vem da API ora DICT
  ora STRING (o proprio `seru_sync` ja tratava os dois na resolucao de
  loja) e o `.get('name')` cru do bloco novo estourava `AttributeError`
  DENTRO do loop que mexe em estoque; sem try/except por pedido a excecao
  escapava do `processar_pedidos`, o `db.session.commit()` do fim nunca
  rodava e as baixas JA FEITAS no ciclo eram descartadas — a cada 15min,
  enquanto o pedido estivesse na janela. Helper canonico
  `seru.nome_company(pedido)` (usar SEMPRE; nunca `.get('company').get(...)`)
  + `_detecta_estorno_pendente` inteiro blindado em try/except: alerta
  best-effort NUNCA derruba o sync. (2) `itens_baixados` devolve
  `(itens, n_fracionarias)` e EXCLUI as baixas '(fracao)'/'(fator' da lista
  — a unidade inteira que fechou no acumulador pode ser de VARIAS vendas
  (por isso o proprio estorno as pula na fase 1), e mandar devolver "1x
  Cookie" que era de 5 cafes criaria estoque fantasma; a mensagem CONTA as
  fracoes com o aviso de nao devolver na mao. (3) env negativa
  (`MAX_MSGS_DIA=-1`) calaria o vigia pra sempre em silencio (`0 >= -1`) —
  `_cfg_int` tem piso zero + WARNING. (4) `_carregar_estado` valida com
  `isinstance` em TUDO (estado torto nao se autocorrige: cegaria o vigia
  ate alguem apagar a chave na mao) e a poda respeita `SYNC_CATCHUP_DIAS`
  (id podado cedo demais re-alertaria sozinho). (5) `_gravar_estado` nunca
  levanta (o docstring do `alertar` promete isso; na rota virava 500).
  (6) rota devolve 502 quando a Seru cai e usa a API publica
  `estado_dedup()`.
- **LIMITACAO ACEITA**: a janela e a do sync (`hoje - SYNC_CATCHUP_DIAS`,
  filtrada por `createdAt`) — cobranca criada dia 20 e cancelada so dia 25
  ja saiu da janela e NAO e detectada. Cobre o caso real (cancelamento no
  mesmo dia ou no seguinte); ampliar custa varredura extra na API.
- Testes: `tests/test_estorno_pendente_vigia.py` (27 casos; Seru e Z-API
  sempre mockadas — incluindo a integracao com `processar_pedidos` e o
  `company` string que reproduzia o critico). Manual de operacao registrado.

## NF de TRANSFERENCIA industria→loja no scan do QR (20/07/2026)

Pedido do dono: NF de transferencia emitida quando o motorista escaneia o
QR de SAIDA do pedido de loja, com a DANFE atrelada ao pedido na tela dele
(fiscalizacao na estrada). Decisoes do dono (20/07, via AskUserQuestion):
valor dos itens = CUSTO calculado da ficha (`custos.py`; MP pelo custo do
cadastro — 'un' = custo POR UNIDADE, mesma semantica de `_custo_por_grama`);
MP ENTRA na NF (kind 'mp' no TinyProdutoMap — curto de proposito, coluna
String(10)); natureza = env `NF_NATUREZA_TRANSFERENCIA` (default
'TRANSFERÊNCIA DE PRODUÇÃO DO ESTABELECIMENTO', texto EXATO do Tiny);
SKU do canal 'transf' com FALLBACK site→b2b (`sku_transferencia`).

- **Schema (2 commits, sonda /api/claude/deploy)**: `Loja` ganhou
  cnpj/inscricao_estadual/endereco estruturado (destinataria de NF-e —
  mesma regua do ClienteB2B; cadastro em RH → Lojas, card "Dados fiscais"
  com badge pronta/incompleta) e `PedidoLoja` o trio de NF do Tiny +
  nf_numero (contrato de `emitir_nf_generico`).
- **Servico**: `app/services/tiny_nf_transf.py`. `emitir_nf(pedido)` —
  guards: status da separacao em diante, loja fiscal completa, todo item
  com SKU (efetivo ou herdado) e custo > 0 (custo zero ABORTA: NF a R$ 0 e
  mentira fiscal — corrigir a ficha). `emitir_apos_coleta` = BEST-EFFORT
  pos-commit no `_handshake_saida` (padrao loja_pagamento): Tiny/SEFAZ
  fora NUNCA trava a saida do caminhao; resultado vira audit `nf_ok`/
  `nf_falha` e a reemissao manual fica no detalhe do pedido (card "NF de
  transferencia" + Ver DANFE).
- **DANFE pro motorista**: botao na tela de SUCESSO do handshake (pagina
  persistente pos-scan — o motorista guarda aberta; rota
  `/handshake/<token>/danfe` com a mesma guarda do sucesso) e no card do
  pedido em `/driver/<token>/pedidos-loja` (rota `driver.pedido_danfe`,
  exige PIN autenticado). Link do Tiny e temporario — resolvido sob
  demanda via `obter_link_nota_fiscal_com_motivo`.
- **SKUs**: tela `/pedidos/tiny-skus-transferencia` (owner; link na area
  Lojas) — universo `_itens_transf` (receitas ativas + produtos ativos +
  MPs pediveis + qualquer item ja usado em PedidoItem); linha sem SKU
  proprio mostra badge "herda <sku>" quando o site/b2b cobrem. Sem SKU
  nem heranca = pendente de verdade.
- CFOP/NCM vem do cadastro do produto no Tiny via SKU + natureza — se o
  CFOP de transferencia (5.152/6.152) sair errado, o ajuste e no Tiny,
  nao aqui. Manual de operacao registrado (secao RODA SOZINHO).
- **POS-REVISAO (fixados)**: documento fiscal NUNCA usa SKU de sugestao
  fuzzy nao confirmada — `_sku_confirmado` exige confirmado_em ou
  salvo-por-humano em TODOS os canais do fallback (o sync da tela transf
  cria auto_match pra todo o universo e um chute herdaria por cima do SKU
  confirmado do site → NCM/CFOP errados na SEFAZ); produto INATIVO em
  pedido antigo usa `calcular_custo_produto` direto (nao aborta com msg
  enganosa); badge "pronta pra NF" do RH usa a MESMA regua da emissao
  (`Loja.fiscal_completo`); NF REJEITADA tem saida na UI ("Refazer do
  zero" com confirm no card do detalhe); voltar_status/cancelar com NF
  emitida AVISAM que a nota nao e desfeita (cancelamento de NF e no Tiny,
  nao ha caminho no sistema); salvar_loja_fiscal trunca no tamanho da
  coluna (texto colado dava DataError/500); DANFE do painel do driver
  exige POSSE do pedido (driver_id); etapa de audit
  'double_submit_suprimido' (23 chars) estourava String(20) em Postgres e
  o evento sumia — virou 'dbl_submit_suprim'.
- **TRADE-OFFS ACEITOS / PENDENCIAS (decisao separada)**: a emissao no
  scan e SINCRONA (Tiny pendurado = spinner de ate ~1min pro motorista no
  pior caso; thread evitaria mas criaria corrida com o botao manual —
  o motor de NF nao tem claim atomico, classe pre-existente do site/B2B);
  `recriar=1` refaz NF ate autorizada (POST forjado — mesma semantica
  documentada do B2B, "risco do gesto"); MP de unidade 'g'/'ml' na NF sai
  com valor POR GRAMA (MPs pediveis hoje sao 'un'; se um dia pedirem MP
  em gramas com quantidade=pacotes, a NF subvaloriza ~1000x — conferir);
  homonimas disputam custo por NOME (fraqueza pre-existente do custos.py,
  agora com consequencia fiscal); a rota publica da DANFE
  (/handshake/<token>/danfe) bate no Tiny a cada GET sem rate limit
  (por desenho: pagina de sucesso persistente); card fiscal do RH lista
  tambem a loja "Industria" (ruido cosmetico).
- **Motivo da REJEICAO da SEFAZ persistido (20/07/2026)**: `PedidoLoja.
  nf_erro` (TEXT, 2 commits). O motor `tiny_nf.emitir_nf_generico` grava o
  erro do Tiny/SEFAZ (`emitir.get('erro')`, ex. "CST com beneficio sem
  cBenef, cod 32") via helper `_set_nf_erro` (hasattr — genérico, site/B2B
  seguem sem a coluna sem quebrar) e LIMPA no sucesso e no `recriar`. Antes
  o motivo só ia no flash e sumia — o dono/contador ficava sem saber o que
  corrigir no Tiny. O card de NF do detalhe mostra badge "✗ rejeitada pela
  SEFAZ" + o texto do erro (o fiscal CST/CFOP/NCM/cBenef vem do cadastro do
  produto no Tiny, NAO do nosso payload — só mandamos SKU+qtd+valor; ajuste
  é no Tiny + contador, depois "Refazer do zero" reemite). Testes:
  `test_rejeicao_sefaz_persiste_o_motivo`, `test_sucesso_limpa_erro_anterior`.
- **Dispensa de NF (20/07/2026, dono)**: `Loja.nf_dispensada` (checkbox no
  card fiscal do /rh/lojas — "não posso dar essa opção pro motorista e o
  padeiro") e `PedidoLoja.nf_dispensada` (toggle ADMIN-only no detalhe;
  rota `pedidos.nf_dispensar`). Fonte única
  `tiny_nf_transf.nf_dispensada_para` (pedido OU loja dispensa): o scan
  pula a emissão SEM tentar o Tiny (audit `nf_disp`, zero espera) e a
  emissão manual recusa até reativar. Dispensar com NF já emitida NÃO
  cancela a nota (aviso; cancelamento é no Tiny). ALTERs pelo procedimento
  de 2 commits (armadilha nova do vigia de deploy: o push-retry REBASEIA e
  o sha local morre — confirmar por CONTEÚDO `git show <sha_no_ar>:arquivo`
  quando a ancestralidade não casar).
- Testes: `tests/test_nf_transferencia.py` (23 casos; Tiny sempre mockado).

## Bot de atendimento — memoria cross-conversa + busca por telefone (19/07/2026)

Relatorio do auditor apontou "bot perdendo contexto e reiniciando do zero
(2x)" + "handoff sem tentar resolver (1/1)". Investigacao confirmou os DOIS
como problemas estruturais (nao de disciplina do modelo); dono aprovou
("Po perder o contexto nao pode ne"). Pacote em 5 pecas:

- **Memoria cross-conversa**: `chatbot_conversa.contato_key` (telefone
  canonizado via `telefone_chave`; ALTER em `migrations_legacy` PG+SQLite
  deployado ANTES do modelo — procedimento de 2 commits, sonda
  /api/claude/deploy). Conversa NOVA do Chatwoot (store E seed vazios) herda
  as ultimas `CONTEXTO_CONTATO_MAX_MSGS=12` msgs da conversa mais recente do
  MESMO contato em `CONTEXTO_CONTATO_DIAS=30` (`chatbot.contexto_do_contato`),
  com marcador "[conversa ANTERIOR deste cliente]" mesclado no ultimo
  assistant (alternancia preservada). `salvar_historico(contato_key=...)`
  indexa; None NUNCA apaga chave ja gravada. O contexto herdado e persistido
  no store da conversa nova no 1º salvar (cap de 40 segura o tamanho);
  marcador `handoff_em` herdado tambem vale pro dedupe (mesmo cliente,
  equipe ja acionada — comportamento desejado).
- **Busca de pedido pelo TELEFONE do canal**: `consultar_pedido` sem numero →
  `bot_tools._pedidos_recentes_por_telefone` (janela 90d, cap 3, filtro
  Python-side — mesmo padrao do card CRM). 1 achado = ficha completa direto;
  2-3 = lista compacta (numero/status/datas, SEM cartinha/itens) pro bot
  perguntar qual e. Fail-closed: SO o telefone verificado do canal localiza
  (mesma credencial da autorizacao existente, cliente OU destinatario);
  NOME nunca busca (sugestao do auditor rejeitada — nao e prova de
  identidade). Prompt (rastreamento/pos-compra/consulta/data-entrega) e
  texto do enforcement anti-preguicoso atualizados: sem numero → buscar por
  telefone ANTES de pedir numero ou transferir.
- **Vassoura store-first**: `varrer_pendentes_sem_resposta` usa o STORE como
  base e anexa so as msgs finais do cliente vindas da API (antes SOBRESCREVIA
  o store com o recorte de 20 msgs do Chatwoot — perdia turnos e marcadores
  handoff_em). `listar_conversas_paradas` agora devolve `telefone` e a
  vassoura passa `telefone_contato` (autorizacao de pedido funciona nesse
  caminho).
- **Lock por conversa CROSS-WORKER**: `crm/routes._lock_conv_cross_worker`
  — `pg_advisory_lock(7753, crc32(conv_id))` bloqueante em volta do
  processamento (o `_BOT_LOCKS` e memoria de UM processo; prod roda gunicorn
  `--workers 2` e mensagens paralelas da mesma conversa faziam last-writer-
  wins no store). No-op fora do Postgres (SQLite = 1 processo). Advisory
  lock 7753 RESERVADO pro chatbot no registro do projeto.
- **Sonda `GET /api/claude/vigia-vereditos`** (?dias=&limite=&conv=&conversa=):
  vereditos do vigia direto do BANCO + store de uma conversa
  (`?conversa=<id>`) — o /admin/vigia/diag e memoria volatil e o relatorio
  do auditor nao traz conv_id; sem isso, investigar achado do auditor de
  fora exigia query manual.

**Pos-revisao (fixados)**: a busca por telefone localiza SO pelo telefone
do COMPRADOR (`telefone_cliente`) — o do DESTINATARIO descobriria o
PRESENTE-SURPRESA (itens + cartinha) perguntando "tem pedido pra mim?"
(mesma classe do caso 13/07); destinatario COM o numero segue autorizado.
Msgs herdadas carregam `herdada: True` (preservada pelo salvar): o
detector de loop as ignora (3 "oi" em conversas diferentes nao e bot) e a
heranca nao encadeia (msg herdada nao re-herda — sem marcador antigo no
meio do contexto); fallback pra penultima conversa se a mais recente
estiver vazia. Vassoura e followup agora seguram os MESMOS dois locks do
webhook em volta do read-modify-write do store (a vassoura corria com
webhook em voo) e a vassoura preserva IMAGENS pendentes + pula conversa
sem conteudo utilizavel (nao re-responde contexto velho). Advisory lock em
AUTOCOMMIT (sem "idle in transaction" no turno) e unlock falho INVALIDA a
conexao (lock preso nunca volta pro pool — conversa travaria pra sempre).
Identifier de canal IG (~17 digitos) nao vira pseudo-telefone (so 10-13
digitos alimentam memoria/busca). TRADE-OFFS ACEITOS: dedupe de handoff
vale CROSS-conversa via marcador herdado (mesmo cliente, equipe ja
acionada — a fila continua garantida pelo status open); conexao dedicada
do lock dobra o uso do pool por conversa ativa (~7 simultaneas por worker;
estouro degrada pra FALLBACK+handoff, monitorar antes de mexer no pool);
telefone reciclado/compartilhado herda contexto do titular anterior
(mesma credencial ja aceita na autorizacao de pedido); sobreposicao
PARCIAL de pendente da vassoura pode duplicar 1 texto no contexto (raro,
so confunde o modelo). Armadilha que custou um ciclo de CI: reescrever
texto do prompt QUEBRANDO frase que teste trava ("pelo seu cadastro" caiu
em quebra de linha) — os testes de prompt (`test_chatbot_faq_pilar_b.py`)
fatiam janelas de N chars; ao editar secao coberta, rodar o arquivo de
teste do prompt antes do push. Testes do pacote:
`tests/test_bot_memoria_busca.py` (17 casos).

## Arquivado NUNCA entra em fluxo ativo (varredura 19/07/2026)

Caso gatilho: "Pao de queijo un" (Receita ARQUIVADA em 01/07, preco atacado
R$ 0,50) aparecendo no /cardapio?tipo=atacado — a query de receitas do
cardapio nao filtrava `arquivada_em` (dono: "como que o sistema coloca algo
arquivado em paginas ativas?"). Varredura sistemica achou a MESMA classe em
~20 pontos; todos corrigidos com os helpers canonicos ja existentes:
`Receita.ativas()` (docstring manda usar em pickers/matchers/seletores),
`Produto.ativo=True`, `MateriaPrima.ativas()`. Historico continua lendo
`query` cru DE PROPOSITO (pedido antigo mostra o que foi vendido).

Pontos corrigidos (alem do /cardapio, que tambem alimenta o PDF):
typeahead do pedido loja→industria (`pedidos/buscar-itens.json`),
`_catalogo_venda` do B2B (com GRANDFATHER no editar: item ja na venda segue
visivel mesmo arquivado), copilot (`_resolver_produto` 3 ramos,
`_resolver_item_qualquer`, `_catalogo_texto`, `consultar_margem` — produto
soft-deletado era resolvivel pra pedido/venda NOVOS), matchers de estoque
em lote (`estoque_loja_lote._carregar_catalogo` + `sugerir_para_pendentes`)
e congelados (`estoque_congelados._carregar_catalogo`) — nome de arquivada
agora vira `nome_pendente` em vez de ressuscitar linha morta, plano manual
(/producao/novo), typeahead do padeiro (produzir), selects de vincular MP
do contas-a-pagar (detalhe + mapeamentos), orfaos de cesta + resolucao por
nome do salvar composicao (homonima arquivada nao amarra mais FK),
dashboard (receita_estimada/margem_geral), /rentabilidade,
/relatorios/custos (+CSV), margem por categoria, /relatorios/ingredientes,
/todo, revisar fotos, categorias da vitrine, tabela de precos por cliente
B2B (faltantes) e **aprovacao de orcamento B2B re-valida** item arquivado/
inativo no gesto (aprovar cria VendaB2B na hora; item morto = erro claro).
Testes: `tests/test_arquivadas_fora_de_fluxo_ativo.py` + regressao do
cardapio em `tests/test_cardapio_atacado_regras.py`. REGRA: picker/matcher/
resolver NOVO usa SEMPRE os helpers `ativas()`/`ativo=True` — e "excluir"
de Produto com historico vira `ativo=False` (soft-delete), entao filtrar so
Receita nunca basta. EXCECAO DELIBERADA: `copilot._resolver_item_qualquer`
(desperdicio/devolucao/retirada) NAO filtra Produto — opera sobre estoque
FISICO ja existente; produto soft-deletado com saldo precisa continuar
escoavel pelo bot. `consultar_margem` tambem nao filtra (leitura — contrato
do catalogo.py). **Pos-revisao (fixados)**: `sugerir_para_pendentes` agora
filtra Produto tambem (sugestao de inativo pre-selecionava opcao que o
dropdown filtrado nem oferecia — o browser caia no 1º alfabetico com o
rotulo mentindo); salvar composicao de cesta tem GRANDFATHER por FK (linha
existente cujo alvo foi arquivado REUSA a FK antiga em vez de orfanar em
silencio — orfao de verdade so em linha NOVA); POST de precos por cliente
B2B recusa preco novo pra item morto (remover segue ok); /todo filtra
Produto.ativo. PENDENCIAS DOCUMENTADAS (decisao separada, nao bloqueiam):
~~receita arquivada com SALDO fisico vivo sem caminho de escoamento~~ —
RESOLVIDO 19/07/2026 (GO do dono "pode arquivar tudo" apos achado real:
5 arquivadas com ~204 mil un de ledger morto, ex. Croissant Nutella
99.971 na Anesio): rota owner `GET /admin/arquivadas-saldo` (dry-run;
`?executar=1` zera) — quantidade → 0 + mov 'ajuste' rastreavel em
EstoqueLoja/EstoqueProducao de receita arquivada OU produto inativo;
linha com `quantidade_reservada` > 0 e PULADA com aviso (reserva de
site). Mesmo padrao do /admin/retencao. Restam: preco especifico
de item arquivado fica invisivel na tela de precos (segue no banco e
ressurge ao desarquivar), POSTs de pedido/venda aceitam id cru sem
re-validar arquivada (o typeahead filtrado cobre o fluxo real; e o que
mantem o editar de pedido antigo com arquivada funcionando — se um dia
validar, precisa de grandfather explicito como o das MPs), e homonimas
(arquivada + recriada) disputam a chave por NOME no mapa de custos
(`custos.py` — fraqueza pre-existente).

## Cardapio (/cardapio) — branding: bairro + logotipo (20/07/2026)

Tela interna (`@login_required`) do cardapio de atacado/loja/site que o dono
manda pros clientes; a impressao oficial e o PDF do servidor
(`app/services/cardapio_pdf.py`, espelho do `main/cardapio.html`).

- **Bairro = Brooklin** (era "Itaim Bibi"): so a BRANDING do cardapio
  (meta desc + hero-tag + rodape em `cardapio.html`; capa + rodape no PDF).
  NAO confundir com os enderecos REAIS de loja — Anesio Pinto Rosa = Itaim e
  legitimo (chatbot_prompt, loja templates, google_reviews). So o cardapio
  mudou de bairro.
- **Logotipo configuravel** no lugar do wordmark "O Pao": AppConfig
  `cardapio_logo_data` = **data URI** (base64) — auto-contido (sobrevive
  deploy, sem host externo; hero usa `data:` que a CSP libera, PDF decodifica
  os bytes). Vazio = cai no texto "O Pao". Upload em
  `/admin/cardapio-atacado/regras` (admin): `_processar_logo_cardapio` com
  checkbox **branco** (default) = converte a marca preta/monocromatica em
  SILHUETA BRANCA sobre transparente (`ImageChops.multiply` de ink×alpha),
  perfeita no hero ESCURO; desmarcado = imagem fiel (PNG com alpha ou JPEG).
  Rotas `cardapio_logo_upload` / `cardapio_logo_remover`. O logo vale pros 3
  tipos (brand-wide), passado a `render_template`/`gerar_cardapio_pdf`.
  Testes: `tests/test_cardapio_atacado_regras.py`. NAO embutir imagem de
  logo colada no chat (nao ha bytes) — o dono sobe o arquivo pela tela.
- **Diagramacao do PDF (feedback do dono 20/07)**: logo na capa a **26mm**
  (era 15, pequeno); e cada categoria fica INTEIRA numa pagina —
  `gerar_cardapio_pdf` estima a altura (`_altura_categoria`) e se a
  categoria nao cabe no espaco restante mas cabe numa pagina limpa
  (`_PAG_UTIL=250`), quebra ANTES do titulo. Efeito: pagina 1 vira a capa,
  cada categoria comeca limpa (sem "Paes" partido no meio de duas paginas).
  Categoria maior que uma pagina flui e quebra por fileira como antes.
- **Descricoes do atacado + metodos de preparo (20/07/2026, ditado do
  dono: "descricao sincera de cada produto b2b, quanto menos e mais...
  fala dos ingredientes... Colocar tambem os metodos de preparo")**:
  - `Receita.descricao_atacado` (Text; procedimento de 2 commits — o 1º
    deploy do ALTER CRASHOU porque o bloco usava `conn.execute` num ponto
    de `_migrate_postgres` onde o `conn` do bloco inicial ja esta FECHADO
    (`ResourceClosedError` no boot; prod nao caiu, deploy anterior seguiu
    servindo). REGRA: bloco novo abaixo do "Migracoes resilientes" usa
    SEMPRE `_cols`/`_try`/sub-conexao propria, NUNCA o `conn` de cima.
  - SEED UNICO na criacao da coluna (`migrations_legacy.
    DESCRICOES_ATACADO_SEED`, 9 receitas B2B com ingredientes REAIS das
    fichas: T65/T45, Callebaut...). Guard = criacao da coluna, nunca
    re-aplica — edicao do dono na ficha manda. So `descricao_atacado IS
    NULL` no seed (redundante com o guard, cinto+suspensorio).
  - Editavel na FICHA da receita (textarea "Descricao (cardapio
    atacado)"); o salvar so mexe se o campo veio no form (POST de
    lote/legado nao apaga); duplicar copia. Aparece SO no
    /cardapio?tipo=atacado — tela (`card-desc`/`list-item-desc` ja
    existiam) e PDF (card `_CARD_H_DESC` / linha `_LINHA_H_DESC` quando a
    categoria tem alguma descricao — `_altura_categoria` acompanha, senao
    o keep-together mente; texto 7pt, 2 linhas com reticencias via
    `_quebrar_2_linhas`). Sonda `/api/claude/receita` expoe o campo.
  - **Metodos de preparo**: AppConfig `cardapio_atacado_preparo` com
    DEFAULT no codigo (`main/routes.py::CARDAPIO_PREPARO_DEFAULT` — os 4
    ditados: backup/congelado cru = melhor qualidade, assado e congelado
    = mais pratico, sourdough congelado 14 fatias, brioche fresco 3
    dias). CONTRATO: chave AUSENTE = default; gravada VAZIA = dono apagou
    de proposito, bloco some. Uma linha por metodo, "Rotulo: texto"
    (split no PRIMEIRO ':'; rotulo >60 chars = linha corrida). Textarea
    na tela de regras do atacado; caixa propria bege (`_box_preparo`,
    altura medida com multi_cell dry_run em BOLD pra nunca cortar; sem
    em-dash — latin-1). So tipo atacado. **Regras + metodos ficam no
    RODAPE, DEPOIS dos produtos** (dono 20/07: "colocar para o rodape e
    trazer os produtos para cima") — na tela os blocos vem apos as
    categorias e no PDF `_box_regras`/`_box_preparo` sao desenhadas no
    FIM do documento (capa nao tem mais caixas; caixa que nao cabe vai
    inteira pra pagina nova).
  - Testes: `tests/test_cardapio_descricoes.py` (13 casos).

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

## Impostos sobre venda nas margens (13/07/2026)

Pedido do dono (da planilha dele): PIS 1,65% + COFINS 7,60% + ICMS 4,00% =
13,25% sobre o preco de venda, descontados nas MARGENS exibidas. Fonte unica
`app/services/impostos.py` — aliquotas em AppConfig (`imposto_pis_pct` etc.),
editaveis no banner da /rentabilidade (POST /rentabilidade/impostos, admin).
Todas as margens exibidas viram LIQUIDAS: /rentabilidade, /relatorios/custos
(+CSV), dashboard (margem_geral), api margem-categoria, copilot
`consultar_margem` e o resumo da ficha da receita (JS: o template seta
`window.CARGA_IMPOSTOS` e o `calcRent` do app.js desconta). E calculo de
EXIBICAO/decisao — nada mexe em preco, pedido ou transacao. Os helpers
`margem_liquida`/`lucro_liquido` recebem `carga` EXPLICITA (quem itera
centenas de receitas busca `carga_venda()` 1x — sem query em loop).
Testes: `tests/test_impostos_margem.py`.

## Projetos v2 — Início orientado a ação (17/07/2026)

Pedido do dono ("traz v2 aqui /projetos/", escolha dele: "fazer a versão 2 da
tela"). A home `/projetos/` (`painel()`) virou o **Início**: bloco **"Agora"**
(Fazendo / Atrasadas / Para hoje / Próximos 7 dias — cada tarefa aparece UMA
vez: fazendo tem seção própria e as demais são particionadas pelo prazo) +
**quadro por área** com os projetos abertos e as tarefas abertas aninhadas
(quick-add por projeto, status do projeto, estrela de foco, comentário). A
home antiga de cards segue viva em **`/projetos/cards`** — NENHUMA rota foi
perdida; a navegação encolheu pra 5 abas (Início/Hoje/Inbox/Kanban/Calendário)
+ dropdown "Mais" (Dia/Foco/Relatório/Templates/Cards/Visão lista) no macro
`nav_views` de `projetos/_partials.html`.

- **✓ concluir em 1 clique**: botão `.proj-done` na linha (`tarefa_linha` no
  `_partials.html`; handler optimistic em `projetos.js` — risca, some e
  sincroniza; erro reverte). O círculo de ciclo de status continua ao lado.
- **Busca** (`#busca-tarefa`) na home filtra tarefa E projeto: projeto cujo
  nome casa mostra todas as tarefas; card sem match some; área sem card some.
  Só age em `.proj-card[data-proj-nome]` — as views antigas não têm o atributo
  e ficam intocadas.
- **Recorrência sem lixo**: `_agendar_proxima` agora tem DEDUPE (não cria
  próxima ocorrência se já existe outra ABERTA com mesmo projeto+nome+
  recorrência — alternar feito→a_fazer→feito duplicava a cada ciclo, lixo real
  no quadro de prod) e `tarefa_mover` (drag do kanban) também dispara a
  recorrência ao cair em feito (antes só o clique/modal disparava).
- **Fix copilot `executar_criar_tarefa`** (`copilot.py`): a versão anterior
  SEMPRE estourava (kwargs `data_prazo`/`criado_por` não existem no modelo e
  `projeto_id=None` viola NOT NULL). Agora usa `prazo=` e, sem projeto que
  case, cai na Inbox via `_get_inbox_projeto` (import lazy do blueprint).
  Endurecido pós-revisão: o match de `projeto_nome` FILTRA áreas privadas
  (igreja/vida) pra quem não é dono (a tool é de funcionário/gerente também —
  sem o filtro dava pra criar tarefa em, e descobrir nome de, projeto privado),
  prefere match exato e depois o nome mais curto; `data_prazo` inválida devolve
  `aviso` em vez de sumir em silêncio.
- Pós-revisão também: projeto `concluido` COM tarefa aberta continua no quadro
  (badge "concluído c/ pendência") — sem isso a tarefa ficava invisível na
  home; e o ✓ risca TODAS as cópias da tarefa no DOM (Agora + quadro).
- Trade-off ACEITO do dedupe: duas tarefas recorrentes distintas com o MESMO
  nome no MESMO projeto — concluir uma não agenda a próxima enquanto a outra
  estiver aberta (chave é projeto+nome+recorrência, não id de origem).
- Testes: `tests/test_projetos_v2.py` (Agora/quadro, partição sem duplicata,
  rotas antigas vivas, 403 não-owner, recorrência 3 casos, copilot 4 casos,
  concluído com pendência).

## Opção "fatiado?" nos sourdoughs no site (16/07/2026)

Pedido do dono: o cliente escolhe, POR ITEM, se o pão sourdough vem fatiado.
So preferencia de corte — **NAO mexe em preco nem estoque** (mesmo SKU, so
cortado); confirmado que baixa/reserva (`baixa_venda`, `loja_estoque_reserva`)
so olham receita/produto/qtd.

- **Quais**: so sourdough de FATIAR. `loja_catalogo.receita_fatiavel(r)` =
  `familia == 'pao_sourdough'` OU nome contem 'sourdough' (os "Mini
  Sourdough" estao com `familia` NULL no cadastro — o nome resgata; NAO usar
  `familia_default()`, que assume NULL→sourdough e pegaria granola/iogurte,
  que tambem sao Receita), EXCETO os paezinhos `_NAO_FATIAVEL_NOME`
  (pao frances, baguete — sao familia sourdough mas nao se fatiam; decisao
  do dono 16/07). Exposto como `fatiavel` em `_serializar_receita`.
- **Marcar no CARRINHO/CHECKOUT tambem (16/07/2026)**: alem do checkbox na
  pagina do produto, cada item fatiavel mostra um checkbox "🔪 fatiado" na
  LINHA do drawer, da pagina do carrinho e do resumo do checkout
  (`fatiadoControle` em carrinho.js + `pintarResumo` em checkout.js).
  `Carrinho.alternarFatiado(kind,id,deFatiado)` move a qtd pro outro estado
  SOMANDO se a linha destino ja existir. Requer `fatiavel` no item resolvido
  (`_resolver_carrinho_sessao`), no `adicionar` (produto + card da vitrine
  via `data-fatiavel` em home.html) — sem isso a linha nao mostra o checkbox.
- **Coluna** `PedidoOnlineItem.fatiado` (Boolean nullable; NULL/False =
  inteiro). PRIMEIRA coluna adicionada a `pedido_online_item` — procedimento
  de 2 commits (ALTER em `migrations_legacy` PG+SQLite deployado ANTES do
  modelo, confirmado por `/api/claude/deploy`).
- **Viagem da escolha** (a sessao e a fonte de verdade e so guardava
  {kind,id,qtd}): `fatiado` entra na CHAVE DE DEDUP (fatiado e inteiro do
  mesmo pao = linhas separadas, nao somam qtd) em `carrinho.js` (_chaveItem/
  adicionar/mudarQtd/qtdDe + payload do sync + render com selo "🔪 fatiado"),
  `loja/routes.py` (_carrinho_sessao/_set_carrinho_sessao/_resolver — sem
  isso a flag some) e `checkout.js` (itens_json). Checkbox so no sourdough
  (`produto.html`, `data-fatiavel`).
- **Sanitizado no SERVIDOR** (`loja_checkout.montar_itens`): `fatiado` so
  vale se o cliente pediu E `cat['fatiavel']` — POST forjado com fatiado=true
  num nao-sourdough e ignorado.
- **Exibicao pra cozinha/cliente**: selo no card do painel de entregas
  (`painel_pedidos.html`), na impressao/PDF do entregador (`imprimir.html`,
  `pdf.py`), no detalhe admin do pedido (`loja_online_pedido_detalhe.html`) e
  "(fatiado)" no e-mail de confirmacao (`email.py`).
- Testes: `tests/test_fatiado_sourdough.py`.

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

## Spotify na tela do padeiro (15/07/2026)

Widget "🎵 Música" no /padeiro com DOIS modos:
- **"🔊 Tocar nesta tela"** (decisão do dono 15/07: "quero que ele toque as
  músicas") — Web Playback SDK: o navegador da tela vira um aparelho
  Spotify ("Tela do Padeiro — O Pão") e o som sai POR ELA. Exige: escopo
  `streaming` (conta conectada antes disso precisa RECONECTAR em
  /admin/spotify), token entregue ao navegador via
  `GET /padeiro/spotify/token` (protegido por papel — exceção consciente ao
  "token nunca no browser": exigência do SDK), CSP do /padeiro afrouxada pro
  Spotify (sdk.scdn.co + *.spotify.com + blob:, ESCOPADA ao path em
  `app/__init__.py`) e navegador com DRM (Chrome/Edge/Firefox/Safari
  desktop; iPad/iPhone NAO suportado pelo SDK). Primeiro toque no botao
  carrega o SDK (gesto libera o áudio) e transfere a reprodução pra tela.
- **Controle remoto** (Spotify Connect) — comandos miram o aparelho ativo
  (ou a tela-player via `device_id` no POST de ação). O SERVIDOR fala com a
  API (`app/services/spotify.py`); rotas `/padeiro/spotify/estado|acao`.

Exige conta PREMIUM (403 PREMIUM_REQUIRED e 404 NO_ACTIVE_DEVICE viram
mensagens claras no widget). Sonda de diagnóstico:
`GET /api/claude/spotify-debug` (presença de envs, conexão, teste do player).
ARMADILHA de config: env var nova SÓ chega no app se declarada em
`config.py` (Flask não absorve environ sozinho — bug real no 1º deploy,
travado por teste `test_config_mapeia_as_envs_spotify`).

- **Conexão da conta**: `/admin/spotify` (admin; link na área Administração)
  — OAuth authorization code com state anti-CSRF na session; refresh token
  em AppConfig (`spotify_refresh_token` etc., sobrevive a deploy). Access
  token renovado sob demanda (rotação de refresh token persistida).
- **Envs**: `SPOTIFY_CLIENT_ID` + `SPOTIFY_CLIENT_SECRET` (app criado pelo
  dono em developer.spotify.com; Redirect URI exata mostrada na tela) +
  opcional `SPOTIFY_REDIRECT_URI`. Sem envs = widget avisa "não
  configurado", nada quebra.
- Testes: `tests/test_spotify.py` (API SEMPRE mockada, padrão Anthropic).

## Patrimônio — inventário de móveis/equipamentos com etiquetas QR (20/07/2026)

Pedido do dono ("preciso fazer o inventario da industria e das lojas...
colar aqueles codigos de barras ou QR Code"; escolha dele no follow-up:
**moveis e equipamentos**, modulo completo). QR escolhido sobre barras 1D:
a camera de qualquer celular le e o QR carrega o LINK da pagina de
conferencia — zero app, zero digitacao.

- **Modelos** `Ativo` + `AtivoConferencia` (`app/models/patrimonio.py`,
  tabelas novas via `db.create_all`, sem ALTER). Ativo: nome, categoria,
  loja_id (NULL = industria), local_detalhe, nº serie, valor (Numeric —
  regra B4), situacao em_uso|manutencao|baixado. Conferencia = "vi este
  ativo" (quem, quando, ONDE viu, ok|problema + obs). `codigo` = A-NNNN.
- **Tela `/patrimonio`** (admin; link na area Administracao): cadastro
  1-a-1 OU lote (textarea um nome por linha), filtros, edicao inline,
  manutencao/baixa/reativar (baixado nunca se apaga — sai das etiquetas e
  do inventario), ultima conferencia por ativo e contador de **nao
  conferidos desde `?desde=`** (default 30d) — o relatorio do inventario.
  Aviso "⚠ visto em X" quando a ultima conferencia divergiu do cadastro
  (conferir NUNCA muda cadastro; mover e gesto do admin).
- **Etiquetas** `GET /patrimonio/etiquetas.pdf?loja=&categoria=&ids=`
  (admin): PDF servidor (`app/services/patrimonio_pdf.py`, fpdf2), grade
  3×7 de 63,5×38,1 mm (padrao Pimaco 21/folha; linhas-guia pra papel
  comum). QR = `<APP_BASE_URL>/patrimonio/<id>/conferir`. Baixados fora.
  `qrcode_svc.gerar_png_bytes` (novo) alimenta o PDF; o data_url antigo
  virou wrapper dele.
- **Conferencia `/patrimonio/<id>/conferir`** (QUALQUER funcionario
  logado — inventario e de todo mundo; cadastro segue admin): pagina
  mobile com "✅ Esta aqui e funcionando" e "⚠ Tem algum problema..."
  (+ select de onde ele esta). Validada a 390px (Playwright + Bootstrap
  local, metodo da casa).
- Manual de operacao: item na secao MENSAL/TRIMESTRAL. POS-REVISAO
  (fixados): parse de valor via `parse_float_br` CANONICO + form re-rende
  em pt-BR — o parse local removia todo '.' e o round-trip da edicao
  inline multiplicava o valor por 100 a cada salvamento (critico, dinheiro
  silencioso); valor invalido MANTEM o gravado com flash (nunca None
  calado); grandfather de loja DESATIVADA nos selects de editar/conferir
  (sem ele o browser caia na 1ª opcao e o ativo "mudava" pra Industria em
  silencio); etiqueta trunca nome (36) e local (42) — nome longo invadia
  a etiqueta de baixo; `ativo` entrou em AUDITED_MODELS (baixa/valor com
  trilha); filtros busca/situacao tambem valem no PDF; teste de paginacao
  do PDF corrigido (>= 3 — a arvore /Pages casava o prefixo e >= 2 era
  vacuo). LIMITACOES CONHECIDAS (baixa, nao bloqueiam):
  `_ultimas_conferencias` carrega todas as conferencias dos ativos
  listados (cresce com anos de inventario — otimizar quando doer) e
  `reativar` limpa `baixado_em` (trilha fica no /audit). Testes:
  `tests/test_patrimonio.py` (18 casos).

## Acesso: senha provisoria forcada + "so treinamento" (23/07/2026)

Duas colunas em `Usuario` (procedimento de 2 commits — ALTER em
`migrations_legacy` deployado ANTES do modelo, sonda `/api/claude/deploy`):

- **`senha_provisoria`** (decisao do dono): a senha gerada no cadastro
  (`treino_acessos.gerar_acesso`, `auth.novo_usuario`, `auth.reset_senha`)
  nasce provisoria. O gate global `app/__init__.py::_gate_conta`
  (`@app.before_request`, roda DEPOIS do roteamento por host) prende o
  usuario em `/auth/minha-senha` ate ele trocar (a senha do e-mail e a
  "atual"; `minha_senha` recusa `nova == atual` e zera o flag no sucesso).
  Reset por admin RE-forca. Motivo de ser `before_request` e nao pos-login:
  `login_user(remember=True)` faz a pessoa voltar autenticada sem repassar
  pelo POST.
- **`somente_treino`** (POR PESSOA — o dono escolheu pessoa, nao cargo, via
  AskUserQuestion): conta marcada so enxerga `/treino`; o gate redireciona
  todo o resto pra `treino.home` (barra por URL tambem, GET+POST;
  `ep.startswith('treino.')` libera o blueprint inteiro). Checkbox no cadastro
  (`/auth/usuarios`) + toggle por linha (`auth.toggle_somente_treino`). A
  `base.html` esconde a navegacao nao-treino pra essas contas (defesa em
  profundidade; "Trocar senha"/"Sair" FICAM). Conta so-treino nao recebe o
  Chatwoot no e-mail (`enviar_boas_vindas(com_chatwoot=...)`).

Allowlist do gate (evita loop): `auth.minha_senha`, `auth.logout`,
`auth.csrf_token_novo`, `static`/`*.static`, `pwa_service_worker`,
`pwa_manifest`, `health`; endpoint None (URL sem rota) passa pra 404.
Guardas: owner nunca e restrito; admin NAO pode marcar a PROPRIA conta
so-treino (auto-lockout — `toggle` recusa `u.id == current_user.id`).
`gerar_acesso` seta so `senha_provisoria` (NAO `somente_treino` — flag
separada, marcada a mao). Testes: `tests/test_acesso_so_treino.py` (12 casos).

## Assinatura eletronica do Regulamento Interno + Importar CONTATOS (05/08/2026)

Pedido do dono: "preciso que os funcionarios assinem o RI" com tres
exigencias dele (verificacao SMS/selfie/gov.br; guardar o log de auditoria;
cada um recebe a copia assinada). Pesquisa verificada (workflow, fontes
2026) mudou a recomendacao inicial:

- **Open source NAO cumpre a exigencia 1**: DocuSeal so tem SMS no Pro
  (US$20/usuario/mes + US$0,20/SMS, passa pela infra deles mesmo
  self-hosted); Documenso 2FA = Enterprise com licenca; OpenSign so OTP por
  e-mail. **Nenhuma plataforma privada assina "com gov.br"** (a API do
  gov.br so e concedida a gestor publico) — o que existe e biometria contra
  base Serpro nas pagas.
- **Base legal** (para não re-pesquisar): Portaria MTP 671/2021 orienta
  assinatura AVANCADA ou qualificada em docs trabalhistas; STJ REsp
  2.159.442/PR (dez/2024) — exigir ICP-Brasil em tudo e formalismo
  excessivo, log+IP+hash+multifator bastam entre particulares. Token via
  WhatsApp ≈ token SMS (posse do numero); o que amarra a prova e o numero
  ser O DA FICHA de registro. Guardar PDF+certificado >= 5 anos.
- **ESCOLHA DO DONO: Autentique** (plano Profissional mensal R$ 99 sem
  fidelidade, docs ilimitados, token WhatsApp/SMS + selfie por creditos;
  depois desce pro gratis ~10 docs/mes). Precos conferidos so por fontes
  secundarias (sites oficiais 403 no proxy) — conferir no checkout.
- **Sonda `/api/claude/funcionarios`** (?todos=1 inclui desligados):
  nome/cpf/funcao/email/telefone/lojas do quadro — criada pra montar o
  lote de assinatura. REVELOU: as fichas estavam SEM contato (41 ativos,
  40 sem e-mail, 41 sem telefone) e o quadro desatualizado (4 desligados
  ainda ativos + 6 contratados sem ficha).
- **Fluxo da coleta**: gerei planilha pro gerente preencher (scratchpad;
  celulas amarelas = falta, aba "e-mails sem dono"); ele devolveu e a
  planilha entra pela tela **`/rh/contatos/importar`** (owner, gate do
  blueprint RH; servico `app/services/contatos_import.py`, espelho do
  folha_import): match por NOME normalizado (planilha nao tem CPF;
  homonimo = aviso e fica FORA), previa antes→depois, campo ilegivel
  (celular fixo, e-mail torto) recusado com aviso SEM derrubar o outro
  campo da linha, vazio NUNCA apaga valor existente, nome fora do quadro
  vira PRE-CADASTRO (promover com CPF depois — nunca cria Funcionario
  direto), linha "desligado" na observacao vira checkbox DESMARCADO
  (desligar e decisao humana). ARMADILHA REAL do parser: a LEGENDA da
  planilha (celula mesclada) contem "e-mail" e "FUNCIONARIO" no mesmo
  texto e era aceita como cabecalho — a deteccao exige as palavras em
  CELULAS DIFERENTES (fixture do teste trava a legenda real).
- **POS-REVISAO (fixados)**: guard isinstance(dict) no `aplicar` (JSON
  forjado nao-objeto dava 500 — classe compartilhada com folha_import,
  que segue com ela); a previa so oferece "novo" que o `aplicar` consegue
  criar (nome completo + e-mail + celular — antes prometia e recusava);
  fichas COMMITADAS antes do loop de pre-cadastros (`precadastro.criar`
  commita e a poda interna pode dar rollback — descartaria as fichas em
  silencio); indices do cabecalho reusados da deteccao (col() re-procurava
  e podia divergir); ficha gravada com 55 nao gera falso "mudou";
  "(011) 9..." com zero de operadora aceito; badge "desligado na ficha"
  na previa de atualizacao; regex de e-mail importada do precadastro
  (validador unico previa==aplicar). ACEITOS com justificativa: sonda
  expoe CPF (a Autentique pede CPF do signatario; Bearer read-only);
  linha desligada com contato preenchido nao gera item de atualizacao
  (re-importar sem a marca resolve).
- **Homonimo com UMA ficha ativa casa nela (1o uso real, 05/08/2026)**: a
  regra original bloqueava QUALQUER nome duplicado e recusou 26 das 43
  linhas — prod tem 25 pares "ficha velha DESLIGADA com CPF placeholder
  (000.000.0XX-XX, de antes da folha da contabilidade) + ficha nova ATIVA
  com CPF real" (a folha de 03/08 nao achou os CPFs falsos e criou fichas
  novas). Nenhum par tem as duas ativas (conferido pela sonda). Agora so
  bloqueia com 2+ ativas ou nenhuma; o desligar de linha homonima mira a
  ATIVA. As 25 fichas placeholder desligadas sao lixo historico inofensivo
  — limpar e decisao separada do dono.
- Testes: `tests/test_contatos_import.py` (25). Manual registrado (QUANDO
  PRECISAR). E-mail corporativo coletivo (contato@opao.online) NAO vale
  pra assinatura individual — controle exclusivo do canal.

## Importar FOLHA DE PAGAMENTO (xlsx) no RH (03/08/2026)

Pedido do dono ("Preciso atualizar o RH", folha 06/2026 da contabilidade). A
folha e MENSAL, entao virou tela em vez de acerto manual:
`/rh/folha/importar` (owner, gate do blueprint RH; link "Importar folha
(xlsx)" no macro rh — NAO confundir com `/rh/folha`, a folha CALCULADA
interna que ja existia). Servico `app/services/folha_import.py`.

- **Match por CPF** (so digitos; `Funcionario.cpf` e unique). Colunas
  localizadas por NOME no cabecalho (a contabilidade muda a ordem);
  aba "Funcionários" ou qualquer aba com coluna CPF.
- **Fluxo**: upload → `ler_folha` (linha sem CPF/salario legivel vira AVISO
  visivel, nunca sumico) → `comparar` (previa: novos / alterados com
  antes→depois / iguais / ativos FORA da folha) → o dono MARCA →
  `aplicar` re-valida tudo contra o banco (a previa e tela, nao autoridade).
- **Regras de peso** (salario e dinheiro): NADA se grava sem checkbox;
  fora-da-folha NUNCA desliga sozinho (ferias/licenca tambem somem de
  folha — checkbox "marcar desligado" POR PESSOA); quem volta pra folha
  desligado e REATIVADO (limpa data_demissao); a folha manda so em
  salario_base/funcao/data_admissao — VT/VR/telefone/loja intactos.
- Linha da previa viaja como JSON em hidden (`|tojson|forceescape`).
- Validado com a folha REAL 06/2026: 38 funcionarios, 0 avisos.
- Testes: `tests/test_folha_import.py` (11 casos). Manual registrado
  (secao QUANDO PRECISAR).

## Pre-cadastro de funcionario por QR (23/07/2026)

Pedido do dono ("formulario QR pra captar nome, sobrenome, e-mail e telefone
de cada funcionario"; escolha dele via AskUserQuestion: **Pre-cadastro no
RH**). O novo funcionario aponta a camera no QR impresso, preenche um form
PUBLICO (sem login) e o admin promove pra `Funcionario` de verdade informando
o CPF que falta.

- **Modelo** `PreCadastroFuncionario` (`app/models/rh.py`): nome/sobrenome/
  email/telefone + `criado_em`/`processado_em`/`funcionario_id`. **Tabela nova
  via `db.create_all`** — SEM ALTER, sem procedimento de 2 commits (so tabela
  nova, nao coluna). Guarda PII (podavel).
- **Servico** `app/services/precadastro.py`: `validar` (nome+sobrenome >=2,
  e-mail por regex, telefone via `wifi_portal._whatsapp_valido` — celular BR,
  com fallback tolerante); `criar` (DEDUP por e-mail entre PENDENTES — mesma
  pessoa reenviando atualiza a linha em vez de duplicar); `pendentes`;
  `promover(pre, cpf)` (cria `Funcionario` com `cadastro_pendente=True`,
  liga `funcionario_id`, marca `processado_em`; CPF vazio/duplicado = erro,
  nada criado); `descartar`. Timezone via `agora()`.
- **Endpoint PUBLICO** `precadastro` (`/cadastro-funcionario` GET+POST):
  form standalone (`templates/precadastro/form.html`, sem `base.html` — o
  captive-portal do wifi ja provou que scripts externos travam a janelinha;
  aqui e so leveza). `@limiter.limit('6 per minute')` no POST, CSRF do
  Flask-WTF ativo, autoescape do Jinja. O gate global `_gate_conta` nao age
  em ANONIMO (endpoint aberto).
- **Tela admin** `/rh/pre-cadastros` (**OWNER-only** — todo o blueprint RH
  passa pelo `_rh_restrito_ao_owner` before_request): QR (via
  `qrcode_svc.gerar_png_data_url`, URL montada de `APP_BASE_URL`) + lista dos
  pendentes com input de CPF -> "Criar" (promover) e "Descartar". Link
  "Pre-cadastro (QR)" no macro `rh` de `_area_nav.html`. Promover redireciona
  pro detalhe do funcionario pra completar cargo/salario.
- Manual de operacao registrado (secao QUANDO PRECISAR). Testes:
  `tests/test_precadastro_funcionario.py` (23 casos; rotas RH usam
  `owner_user`, nao `admin_user`, por causa do gate owner-only do RH).
- **VINCULAR a funcionario EXISTENTE (05/08/2026, pedido do dono)**: quem
  veio da folha JA esta no RH e preencheu o QR so pra informar e-mail —
  o Criar duplicaria a pessoa. `precadastro.vincular(pre, func,
  gerar_acesso_treino=)` leva e-mail/telefone pra ficha (avisa quando
  SUBSTITUI e-mail existente), marca processado e, se pedido, chama
  `treino_acessos.gerar_acesso` na mesma tacada (reusa TODAS as guardas:
  conta de admin com o mesmo e-mail ou e-mail de OUTRO funcionario =
  recusa com aviso, nunca vincula conta errada). `sugerir_funcionario`
  pre-seleciona o select por nome (score >= 0.75, mesmo piso do
  pre-preenchimento do PDV do Tiny; empate = sem sugestao — a sugestao
  NUNCA grava sozinha). Rota `POST /rh/pre-cadastros/<id>/vincular`;
  o Criar antigo segue pra contratacao nova. Conta so-treino continua
  gesto manual em /auth/usuarios (flag `somente_treino`).
  **Conta JA existente (caso Marina, mesmo dia)**: segundo select opcional
  "conta do sistema" — quando a pessoa ja usa o sistema com OUTRO login,
  o vincular faz os DOIS vinculos (pre→RH e RH→Usuario existente) SEM
  criar conta nem mandar senha (login intocado). Guardas em
  `_PAPEIS_VINCULAVEIS` (funcionario/gerente/producao/padeiro/rh; admin/
  owner/marketing recusados) + conta de outro funcionario recusada; o
  select so lista contas vinculaveis SEM funcionario. `gerar_acesso` e
  ignorado quando ha conta escolhida.
- **POS-REVISAO (fixados)**: (1) XSS ARMAZENADO CRITICO — o nome (input
  PUBLICO anonimo) ia num `onsubmit="confirm('...{{nome}}...')"`; Jinja escapa
  `'` pra `&#39;`, mas o browser HTML-DECODIFICA o atributo antes do JS
  parsear, revertendo pro `'` e quebrando a string (`x');alert(...)//` roda na
  sessao do OWNER, que e a unica vitima porque o RH e owner-only). Trocado por
  `data-nome` + handler delegado no `{% block scripts %}` (lido via `.dataset`,
  texto puro, nunca avaliado) — fecha tambem o bug de nome legitimo com
  apostrofo (D'Ávila quebrava o `confirm`). REGRA: dado de origem publica NUNCA
  entra em handler inline nem `<script>`; use `data-*` + JS que trata como
  texto (ou `|tojson` em contexto de script, nunca `|e`, que e p/ HTML).
  (2) `promover` TRUNCA `nome_completo[:200]` (nome[:100]+' '+sobrenome[:100] =
  ate 201 chars estouraria `Funcionario.nome` String(200) = DataError/500 no
  Postgres). (3) `criar` PODA processados > `_PODAR_PROCESSADOS_DIAS`=180d
  (PII/LGPD; best-effort, `synchronize_session='fetch'`).
- **PENDENCIAS ACEITAS (baixa severidade, decisao separada)**: corrida na dedup
  por e-mail (coluna nao-unique; 2 POSTs simultaneos do mesmo e-mail = 2 linhas
  pendentes — impacto so cosmetico, o admin descarta uma); sem teto diario/
  CAPTCHA no form publico alem do `6/min` (alvo de baixo valor, admin descarta
  spam); CPF sem validacao de digito verificador (pre-existente e consistente
  com o cadastro manual em `rh/routes.py` — validar so aqui divergiria).

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
