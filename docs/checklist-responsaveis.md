# Responsáveis do checklist

O checklist consulta a unidade principal e o período de **Organizar equipe**.
Entram funcionários ativos com cargo Gerente, Gerente de loja ou Atendente
chefe, ou cuja conta vinculada já tenha perfil Gerente e que sejam líderes
diretos de pessoas ativas na mesma unidade principal e período. O perfil de
acesso sozinho não define responsabilidade operacional. Gerência geral e RH
não são escalados pelo título. Cargos estruturados têm prioridade sobre a
função legada. A seleção não usa nomes fixos nem altera contas ou permissões.

A manhã aparece na abertura; a tarde no fechamento. Durante o expediente e
na troca de turno aparecem os dois períodos. Múltiplos responsáveis do mesmo
período são exibidos juntos, sem escolher titular ou substituto arbitrariamente.
Não há escala automática de folgas ou horas cadastradas por esta mudança.

Os vínculos são consultados diretamente, sem duplicar o cadastro do RH.
Atualizações em Organizar equipe refletem-se na próxima abertura do checklist.
A escala operacional de posições é outro cadastro e não sobrescreve os vínculos.
Loja sem responsável é mostrada explicitamente; funcionário sem unidade
principal inequívoca ou período válido aparece como pendente no quadro geral.

`/checklist/responsaveis` permite aos administradores conferir todas as lojas
e os acessos pendentes. O hub e o formulário mostram a equipe da loja escolhida.
A loja inicial usa a unidade principal do funcionário, mantendo a escolha
manual e o fallback antigo quando o RH não fornece uma unidade operacional.

O histórico continua registrando quem efetivamente respondeu. Um colega com
acesso existente pode cobrir o turno, e nenhum registro anterior é reatribuído.
Não há envio de mensagens nem ampliação de acessos.

Validação: `tests/test_checklist_responsaveis.py` e regressão em
`tests/test_checklist_loja.py`.
