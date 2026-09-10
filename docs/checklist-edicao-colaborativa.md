# Edição do checklist pelos líderes

Na tela de preenchimento, os líderes podem adicionar pontos e usar **Editar ou excluir ponto**. Cada revisão vale somente para a loja selecionada. Excluir exige a senha da própria conta conectada; o ponto sai dos próximos preenchimentos, mas seus registros anteriores permanecem.

A permissão exige acesso ao checklist, funcionário ativo, unidade principal correspondente e cargo de gerente/atendente chefe, perfil Gerente ou liderança de equipe. Administradores podem revisar qualquer unidade. Poder preencher em cobertura de outra loja não dá permissão de editar seus pontos.

As alterações são gravadas sem recarregar a página. Respostas e arquivos dos demais pontos permanecem; um ponto editado precisa ser respondido novamente. Edições simultâneas são verificadas por versão; o fechamento de uma página desatualizada exige revisão.

`ChecklistItemAjuste` guarda a personalização por item/loja. O modelo global permanece intacto. `ChecklistEdicao` registra autor, horário e valores anteriores/novos, sem senha. As duas tabelas novas são criadas por `_setup_schema` com `db.create_all`, serializado pelo advisory lock existente no PostgreSQL; não há colunas novas em tabelas existentes.

Validação: 67 testes do checklist, suíte geral com 4779 aprovados (2 skips e 3 xpasses) e fluxo completo em navegador com líder de teste. A inclusão de dois testes finais e a serialização por loja foram validadas na suíte específica após a suíte geral.
