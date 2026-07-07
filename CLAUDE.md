# Convenções de trabalho (Claude)

## Branches

- **Desenvolva em**: no branch `claude/*` designado da sessão atual
- **Abra PR para**: `claude/bakery-recipe-cost-system-N4ieR` (é o branch que o Railway acompanha — merge dispara deploy automático em produção)
- **Nunca** force-push nem use `--no-verify` sem autorização explícita

## Deploy

Railway está conectado em `claude/bakery-recipe-cost-system-N4ieR`. Push pra produção = abrir PR e mergear. Só mergeie quando o usuário pedir explicitamente.

## Sistema

Flask + SQLAlchemy + Bootstrap 5. Padaria Opão (gestão completa: receitas, pedidos, entregas, PDV, estoque, RH, copilot com Claude Haiku 4.5).

## Como rodar

- **Local**: `python run.py` → http://localhost:2000. Banco SQLite em `~/.padaria/padaria.db`, criado/populado automaticamente no primeiro boot (`app/seed.py`).
- **Produção**: gunicorn via Procfile/railway.json; PostgreSQL via env `DATABASE_URL`.
- **Smoke test mínimo** (imports + registro de blueprints): `python -c "from app import create_app; create_app()"`.

## Arquitetura

- `app/__init__.py` — app factory (`create_app`), registro dos blueprints e **migrations manuais** (ver abaixo)
- `app/blueprints/<domínio>/` — um blueprint por domínio (receitas, pedidos, producao, pdv, entregas, materias_primas, rh, copilot, bot…). Rotas finas; lógica de negócio fica em services.
- `app/services/` — regras de negócio e integrações (custos, producao, seru, vnda, google_maps, dropbox_storage, copilot, pdf, rotas, audit)
- `app/models.py` — todos os models num arquivo só (~48 classes)
- `app/templates/` + `app/static/` — Jinja2 + Bootstrap 5
- `config.py` — toda configuração vem de env vars (VNDA, Seru/PDV, Google Maps, Dropbox, Anthropic, bot WhatsApp). Nunca hardcode credencial.

## Convenções críticas

- **Migrations são manuais — não há Alembic.** O schema nasce de `db.create_all()`; coluna nova em tabela existente **exige** uma entrada `ALTER TABLE` idempotente no dicionário de migrações em `app/__init__.py`. Sem isso, o banco de produção (Postgres) fica sem a coluna e o app quebra no deploy.
- **Datas em UTC** no banco (`datetime.utcnow`). Conversão para horário local só na exibição.
- **Dinheiro e quantidades em `db.Float`** — é o padrão do projeto, siga-o; cuidado com arredondamento em cálculos de custo. Entrada do usuário em formato BR (vírgula decimal) passa por `parse_float_br` (`app/utils.py`).

## Verificação

Não há suite de testes automatizados. Antes de entregar qualquer mudança não-trivial: rode o app localmente e exercite o fluxo afetado de ponta a ponta (criar o pedido, calcular o custo, gerar o PDF — o que a mudança tocar). Rodar só o smoke test não conta como verificação.

## Fluxo multi-agente (só para tarefas complexas)

Use este fluxo quando a tarefa for complexa: feature nova, refactor, mudança que toca 3+ arquivos ou exige investigar várias partes do sistema. Para correções pontuais (1–2 arquivos), trabalhe direto — subagentes aí só adicionam custo e latência.

1. **Especificação completa primeiro.** Antes de delegar, consolide objetivo, restrições e critério de "pronto". Se algo essencial estiver ambíguo, pergunte antes de começar.
2. **Cascateie a investigação em subagentes paralelos.** Quebre a tarefa em subtarefas de leitura/pesquisa independentes e lance os subagentes (Explore / general-purpose) **numa única mensagem, em paralelo**. Cada um devolve só conclusões (arquivos relevantes, como o fluxo funciona, riscos) — nunca despejo de código.
3. **Consolide e execute você mesmo.** Subagentes não compartilham contexto entre si nem com você; quem escreve o código é o orquestrador, com base no que voltou. Não delegue a escrita de código que precise de visão do todo.
4. **Revisão independente.** Ao terminar a implementação, lance o subagente `revisor` (`.claude/agents/revisor.md`) para criticar o diff com contexto limpo. Ele reporta **todos** os achados com confiança e severidade; a filtragem é do orquestrador, não dele.
5. **Avalie e itere — no máximo 2 rodadas.** Corrija o que for procedente (re-delegando investigação a subagentes se necessário) e rode a revisão de novo. Itere só por bug, caso de borda ou violação das convenções deste arquivo — nunca por preferência de estilo.
6. **Entregue.** Rode a verificação do fluxo afetado (seção acima) e resuma: o que mudou, o que a revisão apontou e o que foi corrigido ou descartado (e por quê).

## Autonomia

- Decisões pequenas (nome de variável, texto de label, qual de duas abordagens equivalentes): decida e anote na entrega, não pergunte.
- Mudança de escopo, ação destrutiva (dropar/alterar dados, deletar arquivo que você não criou) ou merge para produção: sempre pergunte antes.
