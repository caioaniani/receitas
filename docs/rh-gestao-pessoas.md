# Gestão de pessoas — reorganização do RH

## Chefia de RH — acesso individual

Em **Usuários**, o proprietário pode escolher **Gestão de RH — salários e
promoções** para a conta da chefia. Não usar Admin ou Owner para este fim.
O perfil `gestao_rh` é novo e fixo: os usuários com `rh` antigo não ganham
acesso, e a matriz de permissões operacionais não amplia seu alcance.

A entrada é `/rh/`, após a troca obrigatória da senha provisória. A chefia
consulta e altera cadastros, cargos/salários, folha, carreira e promoções;
organiza liderança, unidade/período, ponto, férias, escalas e documentos;
administra cursos e acompanha o treinamento de toda a equipe. Para registrar
observação prática, permanece necessária a identificação em uma ficha de
funcionário. Promoção exige revisar e confirmar: o histórico guarda autor e
data, e não recalcula folhas anteriores.

Não recebe acesso ao financeiro da empresa, vendas, pedidos, produção,
catálogo, integrações, PIN/configuração fiscal de lojas ou ferramentas
operacionais no Slack/Copilot. A interface clássica e a nova exibem somente
os caminhos permitidos; o servidor também bloqueia URLs/POSTs diretos.
Custos e catálogo não são carregados no HTML das páginas, inclusive erros.

Contas, vínculos de acesso, senhas e importação de contatos continuam com o
proprietário. A chefia pode conferir o estado do acesso, mas não gerar/redefinir
senhas nem mudar o e-mail de uma ficha que já tem conta. O formulário explica
esse bloqueio. Pré-cadastro permite criar uma nova ficha; vincular a uma ficha
ou conta existente é tarefa do proprietário.

Somente o proprietário concede/remove este papel ou administra suas
credenciais; administradores comuns não podem assumir a conta por reset.
O cadastro não convida a chefia para o Chatwoot. Para uma pessoa com conta
existente, reutilizar a identidade conferida pelo proprietário, sem duplicar.

### Publicação e retorno seguro

Não há migração de banco. Testar salário/folha/promoção, autoria e replay,
isolamento GET/POST, navegação clássica/nova e preservação dos perfis antigos.
Após CI/deploy, conferir o seletor em Usuários e as telas RH por leitura;
nunca usar folha, promoção ou envio real como teste. A publicação não cria
conta nem muda salário, cargo, senha ou vínculo de ninguém.

Se houver falha de isolamento após conceder uma conta, o proprietário deve
revogar o perfil antes de voltar o código. Não fazer rollback simples para
uma versão que desconhece `gestao_rh`, pois ela não contém seu gate restritivo.

## Fluxo original do proprietário

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
