# Central de cobranças

## Acesso e organização

Financeiro → **Cobranças** (`/cobrancas/painel`), seguindo o layout v2 e mantendo
compatibilidade com o layout clássico. A permissão permanece a mesma do
módulo anterior: usuário autenticado com acesso de administrador.

- **Visão geral:** dashboard exclusivamente de consulta, com links de
  pagamentos e quatro áreas: notas fiscais, boletos, faturas mensais e
  histórico de envio. Nenhum formulário, disparo ou emissão automática.
- **Cobranças:** saldo a receber, vencidas, pagas e canceladas; busca por
  cliente/referência, vencimento, situação do envio e documentos;
  30 cobranças por página (`/cobrancas/`, endereço antigo preservado).
- **Fechamentos:** faturas consolidadas do B2B (`/b2b/faturas`).
- **Banco:** remessa, retorno e correções (`/cobrancas/banco`). As rotas e
  serviços que geram/baixam títulos continuam os existentes.
- **Documentos:** `/cobrancas/fatura/<id>/documentos` ou
  `/cobrancas/parcela/<id>/documentos`, acessível também na fatura/venda.

Os atalhos de notas/boletos aplicam um filtro visível e mantido ao buscar
ou paginar. Cada resultado abre a seção correspondente da venda/fatura;
o usuário decide se e quando emitir. O contador de notas se refere a
**cobranças**, não a documentos fiscais únicos: várias parcelas de uma venda
compartilham a NF. Uma cobrança pode aparecer em mais de uma área; os
contadores de pendências não devem ser somados como valor a receber.

O contador de fechamentos conta clientes mensais ativos com vendas ativas,
até hoje, sem parcelas e sem fatura, cujo total é positivo. Não cria contas.
O atalho do banco separa remessas, rejeições e divergências de saldo de
boletos ainda a preparar. Valores zerados, cancelados ou já quitados não
aparecem como sugestões de geração na lista bancária.

## Saldos e fontes

Uma linha por fatura consolidada, parcela não faturada ou boleto avulso.
Parcelas/vendas vinculadas a uma fatura não são somadas novamente. Cobranças
zeradas não entram; canceladas ficam em filtro próprio e fora dos totais.
Os totais respeitam cliente, período, documentos e envio, antes da paginação.

O saldo vem dos recebimentos efetivamente registrados nas parcelas; uma
fatura legada marcada como paga com recebimento parcial ainda exibe o saldo.
Se há boleto, o vencimento exibido é o dele. A central é uma projeção de
leitura: não altera valores, vencimentos, parcelas, pagamentos ou estoque.
Vendas sem parcela/fechamento continuam no B2B, fora dos títulos a receber.

## Envio e histórico

O envio conjunto exige NF autorizada e boleto disponível, não cancelado,
com valor igual ao saldo. Cobranças pagas e boletos antigos com valor cheio
após pagamento parcial não podem ser reenviados pela nova ação.

O usuário confere o destinatário e confirma o envio. Se o estado local é
apenas `remessa`, precisa confirmar que verificou o registro no banco;
isso **não** muda automaticamente a situação bancária do título.

Os dois PDFs precisam ser obtidos antes do disparo. Não emite NF nova nem
gera outra cobrança. Uma chave única, gravada antes do envio, impede
duplicação por repetição da mesma solicitação. Uma nova tentativa deliberada
usa uma nova abertura da tela. Resultado incerto/interrompido exige conferir
o serviço de e-mail antes de reenviar; não há repetição automática.

`envio_cobranca` registra referência, destinatário, documentos/nomes dos
anexos, usuário, horário, resultado e identificador do provedor. Não guarda
PDFs ou credenciais. A única ação de envio B2B é **NF + boleto juntos**.
Os caminhos antigos de NF isolada, boleto isolado e conjunto redirecionam
para a confirmação unificada; não disparam e-mail por conta própria.
Vendas com várias parcelas exigem a escolha da parcela; vendas faturadas
abrem a fatura consolidada.

Cada novo envio conjunto inclui cópia oculta para `caio@opao.online`,
`dakson@opao.online` e `contato@opao.online`, sem expor esses destinatários
no corpo/CC do e-mail. Se um deles já for o destinatário principal, não é
duplicado na cópia oculta. O histórico guarda a lista efetivamente usada.
Os demais e-mails transacionais não recebem essas cópias.

A tela destaca o último envio conjunto aceito pelo provedor, com data,
hora e destinatário, e oferece **Enviar novamente**. Uma falha posterior
não apaga esse sucesso: fica registrada como outra tentativa. Somente
histórico da mesma NF e boleto confirma o conjunto atual; envios antigos
de apenas um documento continuam visíveis, mas não confirmam o par.
Não se inventam datas, destinatários nem cópias ocultas de envios antigos.

- **Sem histórico:** não existe registro no ERP; não prova que nunca enviou.
- **Aceito pelo serviço de e-mail:** API aceitou com identificador; não
  comprova entrega, leitura ou pagamento.
- **Falha:** recusa ou erro antes do envio.
- **Não confirmado:** resultado incerto; conferir antes de tentar de novo.
- **Iniciado, sem confirmação:** intenção gravada sem resultado final.

Não foram adicionados disparos automáticos, cobranças em massa ou lembretes.

## Divulgação sem cobrança

Na venda B2B, o dono pode abrir **Foi divulgação, sem cobrança?**, informar
o motivo e confirmar. A venda sai dos totais/pendências, do banco, do
contas a receber B2B e dos fechamentos futuros. Permanece consultável no
filtro **Sem cobrança**, sem ser marcada como paga ou cancelada.

A classificação guarda motivo, responsável e horário em `dispensa_cobranca`.
Não apaga itens/parcelas/boletos pendentes, não altera valores ou recebimentos,
não emite documentos e não estorna o estoque. Venda faturada, pagamento
registrado ou boleto numerado/movimentado impedem a classificação simples.
Após classificada, as rotas de boleto, remessa, recebimento e envio conjunto
recusam a cobrança. Não há regra por nome de cliente ou número de venda:
cada classificação depende de confirmação explícita do dono.

Esta alteração não define o tratamento fiscal de uma divulgação: documentos
fiscais eventualmente necessários continuam sendo avaliados na origem.

## Publicação e retorno

Migração aditiva `6d9e3c7a2f10`, após `2b8d4e6f0a1c`: cria somente a tabela
de histórico e seus índices, sem alterar tabelas financeiras existentes.
É idempotente com o `db.create_all()` anterior ao Alembic no startup atual,
que já serializa o setup entre workers PostgreSQL.

Migrações seguintes, também aditivas/idempotentes: `91b6a7d3c820` acrescenta
o JSON anulável de cópias ocultas no histórico; `b7248c1d9e02` acrescenta
o JSON anulável da classificação em `venda_b2b`. Registros existentes
permanecem sem classificação/cópias inventadas. Nenhuma venda é alterada
automaticamente na migração.

Não exige nova variável de ambiente. Reutiliza Tiny, Sicredi e o serviço de
e-mail já configurados. Após publicar, verificar GET da central, banco e
documentos de uma fatura existente. Não usar envio real como smoke test sem
aprovação e confirmação do destinatário.

Para reverter o código, preservar `envio_cobranca`: não executar downgrade
destrutivo dessa tabela, pois ela contém o histórico de comunicação.
Preservar também `dispensa_cobranca`. Se houver classificação em produção,
não retornar a um código que ignore essa classificação: isso recolocaria
divulgações na fila. Nesse caso reverter apenas a parte visual/e-mail,
mantendo os bloqueios de cobrança.

## Validação

`tests/test_central_cobrancas.py` cobre consolidação, saldos parciais,
filtros/paginação, ausência de histórico, autorização/CSRF, destinatário,
dois anexos ou nenhum, idempotência, erro/incerteza, envios legados,
atribuição por parcela, migração idempotente e layout clássico/v2.

`tests/test_dashboard_cobrancas.py` verifica contadores, filtros e links
por etapa, lançamentos zerados, fechamentos sem duplicação, navegação
clássica/v2, autorização e que visitar o dashboard e seus atalhos não
chama emissão fiscal, geração de remessa nem envio de e-mail.

Testes automatizados usam banco isolado e provedores simulados.
`test_cobrancas_envio_unificado.py` cobre cópia oculta, reenvio, falhas após
sucesso, ausência dos botões individuais e migração. `test_cobrancas_dispensa.py`
cobre classificação auditável, preservação financeira/estoque, autorização,
ausência em pendências e bloqueios de geração/envio/recebimento.
Conferência visual local usa dados fictícios, sem rede de saída e com
requisições de escrita bloqueadas. Não modifica dados de produção.
