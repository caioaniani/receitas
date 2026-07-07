# Convenções de trabalho (Claude)

## Branches

- **Desenvolva em**: `claude/continue-controller-conversation-aGS3F`
- **Abra PR para**: `claude/bakery-recipe-cost-system-N4ieR` (é o branch que o Railway acompanha — merge dispara deploy automático em produção)
- **Nunca** force-push nem use `--no-verify` sem autorização explícita

## Deploy

Railway está conectado em `claude/bakery-recipe-cost-system-N4ieR`. Push pra produção = abrir PR e mergear.

## Sistema

Flask + SQLAlchemy + Bootstrap 5. Padaria Opão (gestão completa: receitas, pedidos, entregas, PDV, estoque, RH, copilot com Claude Haiku 4.5).

## Fluxo multi-agente (só para tarefas complexas)

Use este fluxo quando a tarefa for complexa: feature nova, refactor, mudança que toca 3+ arquivos ou exige investigar várias partes do sistema. Para correções pontuais (1–2 arquivos), trabalhe direto — subagentes aí só adicionam custo e latência.

1. **Especificação completa primeiro.** Antes de delegar, consolide objetivo, restrições e critério de "pronto". Se algo essencial estiver ambíguo, pergunte antes de começar.
2. **Cascateie a investigação em subagentes paralelos.** Quebre a tarefa em subtarefas de leitura/pesquisa independentes e lance os subagentes (Explore / general-purpose) **numa única mensagem, em paralelo**. Cada um devolve só conclusões (arquivos relevantes, como o fluxo funciona, riscos) — nunca despejo de código.
3. **Consolide e execute você mesmo.** Subagentes não compartilham contexto entre si nem com você; quem escreve o código é o orquestrador, com base no que voltou. Não delegue a escrita de código que precise de visão do todo.
4. **Revisão independente.** Ao terminar a implementação, lance o subagente `revisor` (`.claude/agents/revisor.md`) para criticar o diff com contexto limpo. Ele reporta **todos** os achados com confiança e severidade; a filtragem é do orquestrador, não dele.
5. **Avalie e itere — no máximo 2 rodadas.** Corrija o que for procedente (re-delegando investigação a subagentes se necessário) e rode a revisão de novo. Itere só por bug, caso de borda ou violação das convenções deste arquivo — nunca por preferência de estilo.
6. **Entregue.** Rode os testes/verificação do fluxo afetado e resuma: o que mudou, o que a revisão apontou e o que foi corrigido ou descartado (e por quê).
