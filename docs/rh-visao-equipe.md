# Visão da equipe e histórico de carreira

## Navegação

Disponível ao dono em **Equipe → Visão da equipe** (`/rh/equipe`), na área RH,
no cadastro de funcionários e na busca. Os demais papéis não recebem acesso.
Cada pessoa abre seu histórico em `/rh/funcionarios/<id>/carreira`.

## Promover pela equipe

O botão **Promover** aparece na linha de cada funcionário ativo e no histórico.
Abre `/rh/funcionarios/<id>/promover`: escolher novo cargo e data, revisar os
valores e **Confirmar promoção**. Não depende de editar a ficha completa.

A revisão não grava nada. A confirmação atualiza cargo, função e base da ficha,
sincroniza o enquadramento existente e registra o histórico com autor, data e
cargo/nível anterior e novo, na mesma transação. Preserva premiação, benefícios,
liderança, lojas e conta de acesso. A regra de confiança (40% da base) é mantida;
seu valor acompanha a base nova. O total exibido exclui VT, VR, horas extras e
descontos. O cargo escolhido entra em vigor no cadastro ao confirmar; a data
informada documenta a promoção, não agenda alterações ou recalcula folhas fechadas.

Somente o dono pode revisar/confirmar. O servidor exige cargo ativo diferente,
funcionário ativo e data entre admissão e hoje. Se existe enquadramento, o novo
cargo precisa de faixa inequívoca; vínculos ausentes/ambíguos são recusados.
Não cria faixa ou enquadramento por suposição nem libera acessos automaticamente.
Prévia assinada expira em 30 minutos e está vinculada ao autor/pessoa. A confirmação
relê funcionário e dependências sob lock e rejeita mudanças desde a revisão ou
reenvio de uma promoção já aplicada. CSRF obrigatório nas duas etapas.

Testes adicionais: `test_rh_promocao.py` e `test_rh_promocao_routes.py`, incluindo
prévia sem escrita, preservação dos demais campos, rollback, duplicação e revisão
desatualizada. A publicação disponibiliza o fluxo; promover alguém é uma decisão
explícita posterior na tela.

O painel inclui funcionários do RH mesmo sem login. Usa o cargo efetivo da ficha
e sua faixa de carreira, nunca o nível proposto em um enquadramento ainda não
aplicado. Quando o vínculo é inexistente ou ambíguo, mostra “Nível não definido”.
Direção não conta como pendência de nível ou de liderança.

Filtros: pessoa/cargo, unidade, líder direto, nível, pendência e ativos/inativos.
Métricas consideram a equipe ativa (ou todos, se escolhido); a tabela mostra o
recorte filtrado. A distribuição por cargos fica recolhida por padrão.

## Aprovar o enquadramento em lote

Em **Plano de cargos e carreira → Enquadramento da equipe**, o dono pode marcar
várias pessoas e clicar em **Aprovar selecionados**. **Todos** marca somente os
cadastros disponíveis na lista filtrada; ninguém começa selecionado. O contador
mostra a seleção e **Voltar à seleção** permite ajustá-la antes de confirmar.

A revisão compara cargo, base e total de referência atuais com os que serão
aplicados no RH. O total soma base, confiança e premiação, sem VT/VR/HE/descontos;
não é a remuneração alvo da planilha. Fora da trilha, somente a decisão é aprovada.
Já aprovados, inativos e faixas sem cargo válido não entram. A ação individual
existente permanece disponível.

`plano_carreira_lote` relê e bloqueia todos os registros envolvidos antes da
confirmação, comparando o estado assinado da revisão (30 minutos, por autor).
Se algo mudou, nenhum item do lote é aprovado. Cada aprovação gera uma entrada
`aplicacao_plano` de origem `aprovacao_lote`, inclusive sem troca de cargo; não
gera uma data de promoção. O commit é único e preserva benefícios, acesso,
lojas e liderança. Testes `test_plano_carreira_lote*` cobrem serviço e rotas.

## Atendente = Atendente 1

O painel agrupa ambos os nomes como **Atendente 1**. A consolidação dos cadastros
é uma ação explícita em `/rh/cargos/unificar-atendentes`: primeiro mostra a
prévia, depois um POST protegido por login, owner e CSRF.

Mantém o cargo ativo mais utilizado, conserva seu salário, transfere vínculos
de funcionários/faixas e preserva a união das trilhas obrigatórias. Duplicados
ficam inativos, não são apagados. A operação é idempotente e auditada; recusa
qualquer transferência que alteraria o salário efetivo de uma pessoa, inclusive
inativa. Atendente 2 e Atendente Chefe não são sinônimos de Atendente 1.

Reimportações do plano preferem o cadastro equivalente ativo. Associações
legadas não devem reativar cadastros duplicados inativos.

## Histórico

`rh_movimentacao` é uma tabela nova, exportada no registro de modelos e criada
pelo fluxo existente de `create_all` protegido no startup. Sem ALTER em tabelas
existentes, sem backfill de promoções e sem alterar folhas/salários existentes.

Cada movimentação guarda pessoa, cargos/níveis anteriores e novos como
fotografias independentes das faixas importadas, autor, origem, data de registro
e data efetiva opcional. Reimportar o plano não apaga o histórico.

- Editar cargo na ficha: **Alteração de cargo**, não uma promoção presumida.
- Aplicar enquadramento aprovado: **Aplicação do plano**.
- Informar uma promoção já ocorrida: **Promoção informada**, com data efetiva.

A última promoção do painel considera somente promoções com data informada.
“Sem registro” não quer dizer “nunca foi promovido”. A data de cadastro não é
usada como substituta. Registrar uma data histórica não muda cargo, salário,
liderança nem permissões. O servidor rejeita datas futuras ou anteriores à
admissão e evita duplicata pessoa/data/cargo. Não há exclusão dos registros pela
interface nesta etapa.

## Padrão visual e verificação

CSS isolado em `rh-equipe.css`, tokens do shell v2, tabela no desktop e cartões
no celular; filtros e expansão funcionam sem JavaScript. Estados vazio, sem
nível, sem líder, sem histórico e inativo explícitos. Links têm nomes acessíveis;
ações possuem foco visível. Formulários usam a proteção CSRF existente.

Testes em `test_rh_equipe.py`, `test_rh_cargos.py`, `test_rh_movimentacao.py` e
`test_plano_carreira.py` cobrem autorização, isolamento entre proposta/real,
filtros, histórico, equivalência de cargos, preservação salarial e reimportação.

Prévia local opcional: `tmp/rh-equipe-preview.py` usa SQLite temporário e dados
fictícios em 127.0.0.1:5096, sem seeds, cron ou conexão com produção. Esse script
de demonstração não é importado pela aplicação.

## Publicação e retorno

Publicar somente os arquivos de aplicação, testes e esta documentação; nunca
incluir o banco temporário, script de login da prévia ou dados demonstrativos.
Aprovação do dono recebida em 10/09/2026. Base de produção confirmada:
`claude/continue-controller-conversation-aGS3F` (não o default histórico do GitHub).

Validar CI, status de deploy e leitura autenticada do painel/histórico antes de
dar a publicação por concluída. A consolidação de cargos continua sendo uma
ação separada e explícita: publicar código não consolida cadastros.

Se o painel ou a ficha falhar após publicar, retornar ao commit anterior pelo
fluxo normal de revert/deploy. Não excluir `rh_movimentacao`: a versão anterior
ignora a tabela e os registros precisam permanecer preservados. Um eventual
retorno de código não desfaz consolidações realizadas pelo RH, que têm auditoria
própria. Nenhum salário é alterado pelo startup ou pelo registro de histórico.
