# Gestão de pessoas — reorganização do RH

O dono entra em **Equipe → Visão geral** ou `/rh/`; o cartão RH da Home
também leva a esse painel. Uma única navegação liga Visão geral, Lojas e
equipes, Pessoas, Treinamento e Administrativo. A interface clássica conserva
seus caminhos. Não há concessão nova a gerentes ou ao cadastro delegado.

## Rotina do dono e dos líderes

- **Diariamente, dono:** começar pela loja, conferir pessoas por período e
  abrir o detalhe existente da equipe. Lotação usa unidade principal, sem
  duplicar vínculos adicionais ou contar direção em cada loja.
- **Quando houver pendência, dono:** abrir o assunto e ir à pessoa. As listas
  mostram conta ausente, senha provisória pendente, nível indefinido e estados
  de início/retomada do treinamento. O total deduplica pessoas entre assuntos.
  Ausência de atividade em aula não é nota de desempenho.
- **Acompanhamento, líder:** usar o treinamento no escopo já permitido,
  distinguindo aula assistida, avaliação e observação prática. Não existe uma
  fila formal de solicitações de prática: ausência de registro não implica
  que o líder já deveria ter validado. Nenhuma promoção é automática.
- **Por pessoa, dono:** o nome abre Resumo; Cadastro preserva o editor,
  Treinamento mostra etapas, Acesso explica o vínculo e Histórico conserva
  eventos anteriores. Acesso identifica o e-mail corrigido pela regra canônica.
  Não há data de primeiro login/entrega de convite inventada a partir da senha.
- **Quando necessário, dono:** Administrativo mantém folha, cargos, atestados,
  feedback, aniversários e os demais caminhos anteriores. Salários não aparecem
  na nova entrada nem no resumo da pessoa; continuam restritos nas rotas de RH.

## Reenvio de acessos

A lista leva à revisão GET `/rh/funcionarios/acessos/revisar`, respeitando
unidade/pessoa selecionadas. Somente funcionários ativos com conta e e-mail
válido/sem conflito são elegíveis; o proprietário é excluído. “Pendentes”
inclui apenas senha provisória. Filtros locais de busca da lista não são uma
seleção de destinatários: a revisão mostra expressamente o recorte e os nomes.

A revisão não envia, não salva e não gera senha. O POST exige CSRF, palavra de
confirmação e revisão assinada pelo servidor, vinculada ao autor/modo/IDs e
estado, válida por 30 minutos. Alteração no destinatário, conta, senha, unidade
ou elegibilidade obriga nova revisão; novas pessoas não são incluídas no lote.

`rh_reenvio_acesso_execucao` é uma tabela nova, criada pelo startup serializado
existente (`db.create_all`). Guarda somente nonce consumido, autor, horário e
quantidade — sem credenciais ou dados de contato. PK bloqueia repetir a mesma
revisão entre workers. Antes de cada envio, Funcionario e Usuario são relidos
sob locks separados; o hash revisado deve continuar igual, evitando duas
revisões simultâneas enviarem novas senhas para a mesma conta.

Envios são confirmados individualmente. Falha não desfaz os anteriores.
Se o worker cair, a revisão permanece consumida: conferir os resultados no
provedor antes de preparar outra. Não há reexecução automática nem confirmação
de entrega ao destinatário — somente aceitação pelo provedor.

## Verificação e publicação

Cobertura: leituras sem escrita; contagem canônica; owner-only; editor e
clássico preservados; identificador corrigido; filtros; destinatários por loja;
token ausente/adulterado/expirado/outro autor; mudança após revisão; replay;
duas revisões da mesma conta; falha do provedor. Prévia visual com dados
fictícios e POST bloqueado, sem conexão com produção.

Publicar somente depois de testes, revisão e CI. Validar GET das novas telas
em produção; não usar envios reais como smoke test. Voltar o código por revert
se aparecer erro de acesso/renderização; manter a tabela de execuções para
preservar auditoria. Nenhuma folha, vínculo, senha ou promoção é alterada pelo
deploy. A aprovação para publicar não é autorização para executar um lote.
