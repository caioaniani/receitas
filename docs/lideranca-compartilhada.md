# Liderança compartilhada

Em Organizar equipe, escolha a equipe de referência e outro líder em **Liderança compartilhada**. Os líderes diretos dos integrantes permanecem os mesmos. O parceiro ganha acesso a Minha equipe, progresso e observação prática dos integrantes dessa equipe na mesma unidade principal e período.

O compartilhamento não é transitivo: compartilhar a equipe de Sabrina com Kelvin não compartilha outras equipes que Kelvin eventualmente lidere. Integrantes novos seguem o vínculo direto de Sabrina, respeitando unidade e período. Desligamento, conta desvinculada, loja inativa ou mudança de unidade/período suspende o compartilhamento. Remover a concessão desativa o registro e guarda autor e data.

A gestão usa a permissão existente `pode_organizar_equipe`. O organograma apresenta a liderança compartilhada junto aos dois nomes e mantém a árvore de vínculos diretos.

A tabela nova `EquipeLiderCompartilhado` é criada por `_setup_schema`/`db.create_all` com o lock de inicialização existente. Não há coluna nova em tabela existente.

Também foi incluída a edição de checklist entre as rotas permitidas para contas restritas ao treinamento que já tenham liberação explícita para checklist; a validação de líder e unidade continua na rota de edição.
